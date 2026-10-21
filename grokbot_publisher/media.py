"""Media preparation: make each file fit each platform's spec.

Principle: touch the media as little as possible. A file that already meets
the spec is used as-is. A file that only lacks "faststart" is remuxed (no
quality loss, seconds). Only out-of-spec files are re-encoded. Results are
cached by (source file, spec), so Instagram and TikTok share one encode and a
retry after a failure doesn't re-encode on the Switch's slow CPU.

Duration is never "fixed" by cutting: an approved video is posted whole or
not at all, so a too-long video is a validation error.
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import shutil
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from grokbot_publisher.errors import ConfigError, ValidationError
from grokbot_publisher.models import MediaItem

log = logging.getLogger("grokbot_publisher.media")

MB = 1024 * 1024
CACHE_VERSION = "1"


@dataclass(frozen=True)
class ImageSpec:
    name: str
    max_bytes: int
    max_long: int
    min_aspect: float | None = None     # width / height
    max_aspect: float | None = None
    formats: tuple[str, ...] = ("JPEG",)  # Pillow format names accepted untouched


@dataclass(frozen=True)
class VideoSpec:
    name: str
    max_long: int
    max_short: int
    max_bytes: int
    min_fps: float = 0.0
    max_fps: float = 60.0
    target_fps: int = 30
    max_kbps: int = 10000
    pad_to: tuple[int, int] | None = None  # force an exact canvas, e.g. (1080, 1920)
    require_h264: bool = True
    faststart: bool = True


@dataclass
class Prepared:
    path: Path
    kind: str                     # image | video
    mime: str
    size: int
    width: int = 0
    height: int = 0
    duration: float = 0.0
    source: MediaItem | None = None
    notes: list[str] = field(default_factory=list)


class MediaTool:
    def __init__(self, settings, cache_dir: Path | None = None):
        self.ffmpeg = settings.ffmpeg
        self.ffprobe = settings.ffprobe
        self.preset = settings.ffmpeg_preset
        self.threads = settings.ffmpeg_threads
        self.cache_dir = Path(cache_dir or settings.cache_dir)

    # ---------------------------------------------------------------- probing
    def _require(self, binary: str) -> str:
        found = shutil.which(binary)
        if not found:
            raise ConfigError(f"{binary} not found; install ffmpeg (apt install ffmpeg)")
        return found

    def probe(self, path: Path) -> dict:
        cmd = [
            self._require(self.ffprobe), "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise ValidationError(f"can't read media {path.name}: {proc.stderr.strip()[:300]}")
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        fmt = data.get("format", {})
        info = {
            "format": fmt.get("format_name", ""),
            "duration": float(fmt.get("duration") or 0.0),
            "size": int(fmt.get("size") or path.stat().st_size),
            "bit_rate": int(fmt.get("bit_rate") or 0),
            "has_video": video is not None,
            "has_audio": audio is not None,
            "acodec": (audio or {}).get("codec_name", ""),
        }
        if video:
            width, height = int(video.get("width") or 0), int(video.get("height") or 0)
            if abs(_rotation(video)) in (90, 270):
                width, height = height, width
            info.update(
                width=width,
                height=height,
                vcodec=video.get("codec_name", ""),
                pix_fmt=video.get("pix_fmt", ""),
                fps=_fps(video.get("avg_frame_rate")) or _fps(video.get("r_frame_rate")),
            )
            if not info["duration"]:
                info["duration"] = float(video.get("duration") or 0.0)
        return info

    # ----------------------------------------------------------------- caching
    def _cache_path(self, src: Path, spec_repr: str, card_id: str, suffix: str) -> Path:
        st = src.stat()
        key = hashlib.sha1(
            f"{CACHE_VERSION}|{src.resolve()}|{st.st_size}|{st.st_mtime_ns}|{spec_repr}".encode()
        ).hexdigest()[:16]
        return self.cache_dir / card_id / f"{src.stem}.{key}{suffix}"

    # ------------------------------------------------------------------ video
    def prepare_video(self, item: MediaItem, spec: VideoSpec, card_id: str) -> Prepared:
        src = item.path
        info = self.probe(src)
        if not info.get("has_video"):
            raise ValidationError(f"{item.file} has no video stream")
        reasons = video_problems(info, spec)
        notes: list[str] = []

        if not reasons and spec.faststart and not is_faststart(src):
            out = self._cache_path(src, f"remux|{spec.faststart}", card_id, ".mp4")
            if not out.exists():
                self._run_ffmpeg(
                    ["-i", str(src), "-map", "0", "-c", "copy", "-movflags", "+faststart"], out
                )
            notes.append(f"{item.file}: remuxed for streaming (faststart), no re-encode")
            return self._prepared_video(out, item, notes)

        if not reasons:
            return self._prepared_video(src, item, notes, info)

        out = self._cache_path(src, repr(spec), card_id, ".mp4")
        if not out.exists():
            log.info("media: re-encoding %s for %s (%s)", item.file, spec.name, "; ".join(reasons))
            self._encode(src, info, spec, out)
            final = self.probe(out)
            if final["size"] > spec.max_bytes:
                # One more pass at a bitrate computed to fit the size cap.
                kbps = int(spec.max_bytes * 8 / max(final["duration"], 1) / 1000 * 0.9) - 128
                if kbps < 300:
                    out.unlink(missing_ok=True)
                    raise ValidationError(f"{item.file} can't fit {spec.max_bytes / MB:.1f} MB at a watchable bitrate")
                self._encode(src, info, spec, out, kbps=kbps)
        notes.append(f"{item.file}: re-encoded for {spec.name} ({'; '.join(reasons)})")
        return self._prepared_video(out, item, notes)

    def _prepared_video(self, path: Path, item: MediaItem, notes: list[str], info: dict | None = None) -> Prepared:
        info = info or self.probe(path)
        return Prepared(
            path=path, kind="video", mime=_video_mime(path), size=path.stat().st_size,
            width=info.get("width", 0), height=info.get("height", 0), duration=info.get("duration", 0.0),
            source=item, notes=notes,
        )

    def _encode(self, src: Path, info: dict, spec: VideoSpec, out: Path, kbps: int | None = None) -> None:
        filters = []
        if spec.pad_to:
            w, h = spec.pad_to
            filters.append(f"scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2")
            filters.append(f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black")
        else:
            landscape = info.get("width", 0) >= info.get("height", 0)
            max_w, max_h = (spec.max_long, spec.max_short) if landscape else (spec.max_short, spec.max_long)
            filters.append(
                f"scale='min({max_w},iw)':'min({max_h},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2"
            )
        fps = info.get("fps") or 0
        if fps and (fps > spec.max_fps + 0.5 or fps < spec.min_fps - 0.5):
            target = spec.target_fps if fps > spec.max_fps else max(spec.min_fps, spec.target_fps)
            filters.append(f"fps={target}")
        filters += ["setsar=1", "format=yuv420p"]

        rate = kbps or spec.max_kbps
        args = ["-i", str(src), "-map", "0:v:0", "-map", "0:a:0?", "-vf", ",".join(filters),
                "-c:v", "libx264", "-preset", self.preset, "-profile:v", "high"]
        if kbps:
            args += ["-b:v", f"{rate}k"]
        else:
            args += ["-crf", "21"]
        args += ["-maxrate", f"{rate}k", "-bufsize", f"{rate * 2}k", "-g", "60",
                 "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
                 "-movflags", "+faststart", "-threads", str(self.threads)]
        self._run_ffmpeg(args, out)

    def _run_ffmpeg(self, args: list[str], out: Path) -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.stem + ".part" + out.suffix)
        cmd = [self._require(self.ffmpeg), "-hide_banner", "-loglevel", "error", "-y", *args, str(tmp)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            tmp.unlink(missing_ok=True)
            raise ValidationError(f"ffmpeg failed: {proc.stderr.strip()[-400:]}")
        os.replace(tmp, out)

    # ------------------------------------------------------------------ image
    def prepare_image(self, item: MediaItem, spec: ImageSpec, card_id: str) -> Prepared:
        from PIL import Image, ImageOps

        src = item.path
        reasons = []
        try:
            im = Image.open(src)
            im.load()
        except Exception:
            im = self._image_via_ffmpeg(src, card_id)
            reasons.append(f"{src.suffix.lstrip('.').upper() or 'unknown'} format -> JPEG")
        with im:
            fmt = im.format or ""
            w, h = im.size
            orientation = 1
            try:
                orientation = im.getexif().get(0x0112, 1)
            except Exception:
                pass
            if orientation not in (0, 1):
                reasons.append("EXIF rotation")
            if fmt not in spec.formats and not reasons:
                reasons.append(f"{fmt or 'unknown'} -> JPEG")
            if im.mode not in ("RGB", "L"):
                reasons.append(f"{im.mode} colour mode")
            aspect = w / h if h else 1
            if (spec.min_aspect and aspect < spec.min_aspect - 0.005) or (
                spec.max_aspect and aspect > spec.max_aspect + 0.005
            ):
                reasons.append(f"aspect {aspect:.2f} outside {spec.min_aspect}-{spec.max_aspect}")
            if max(w, h) > spec.max_long:
                reasons.append(f"{w}x{h} larger than {spec.max_long}px")
            if src.stat().st_size > spec.max_bytes:
                reasons.append(f"over {spec.max_bytes // MB} MB")
            if not reasons:  # already compliant (animated GIFs included): use the original
                return Prepared(path=src, kind="image", mime=_image_mime(fmt), size=src.stat().st_size,
                                width=w, height=h, source=item)

            out = self._cache_path(src, repr(spec), card_id, ".jpg")
            if not out.exists():
                img = ImageOps.exif_transpose(im)
                img = _flatten(img)
                img = _pad_aspect(img, spec.min_aspect, spec.max_aspect)
                if max(img.size) > spec.max_long:
                    img.thumbnail((spec.max_long, spec.max_long), Image.LANCZOS)
                _save_jpeg_under(img, out, spec.max_bytes)
            with Image.open(out) as done:
                ow, oh = done.size
        return Prepared(
            path=out, kind="image", mime="image/jpeg", size=out.stat().st_size, width=ow, height=oh,
            source=item, notes=[f"{item.file}: converted for {spec.name} ({'; '.join(reasons)})"],
        )

    def _image_via_ffmpeg(self, src: Path, card_id: str):
        """Fallback decoder (e.g. HEIC from iPhones) when Pillow can't read it."""
        from PIL import Image

        out = self._cache_path(src, "decode-png", card_id, ".png")
        if not out.exists():
            try:
                self._run_ffmpeg(["-i", str(src), "-frames:v", "1"], out)
            except (ValidationError, ConfigError) as exc:
                raise ValidationError(f"can't read image {src.name}; convert it to JPEG or PNG ({exc})") from exc
        im = Image.open(out)
        im.load()
        return im


def video_problems(info: dict, spec: VideoSpec) -> list[str]:
    """Why a video doesn't meet the spec (empty list: use it untouched)."""
    reasons = []
    if "mp4" not in info.get("format", "") and "mov" not in info.get("format", ""):
        reasons.append(f"container {info.get('format')}")
    if spec.require_h264 and info.get("vcodec") != "h264":
        reasons.append(f"codec {info.get('vcodec')}")
    if spec.require_h264 and info.get("pix_fmt") not in ("yuv420p", "yuvj420p"):
        reasons.append(f"pixel format {info.get('pix_fmt')}")
    if spec.require_h264 and info.get("has_audio") and info.get("acodec") != "aac":
        reasons.append(f"audio {info.get('acodec')}")
    w, h = info.get("width", 0), info.get("height", 0)
    if max(w, h) > spec.max_long or min(w, h) > spec.max_short:
        reasons.append(f"{w}x{h} too large")
    if spec.pad_to:
        tw, th = spec.pad_to
        if h == 0 or abs(w / h - tw / th) > 0.01:
            reasons.append(f"{w}x{h} not {tw}:{th}")
    fps = info.get("fps") or 0
    if fps and (fps > spec.max_fps + 0.5 or fps < spec.min_fps - 0.5):
        reasons.append(f"{fps:.0f} fps")
    if info.get("size", 0) > spec.max_bytes:
        reasons.append(f"over {spec.max_bytes // MB} MB")
    return reasons


def is_faststart(path: Path) -> bool:
    """True if the MP4 'moov' atom comes before 'mdat' (needed for streaming)."""
    try:
        with path.open("rb") as fh:
            while True:
                header = fh.read(8)
                if len(header) < 8:
                    return False
                size, kind = struct.unpack(">I4s", header)
                if kind == b"moov":
                    return True
                if kind == b"mdat":
                    return False
                if size == 1:
                    size = struct.unpack(">Q", fh.read(8))[0]
                    fh.seek(size - 16, 1)
                elif size == 0:
                    return False
                else:
                    fh.seek(size - 8, 1)
    except (OSError, struct.error):
        return False


def _rotation(stream: dict) -> int:
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            try:
                return int(float(side["rotation"]))
            except (TypeError, ValueError):
                pass
    try:
        return int((stream.get("tags") or {}).get("rotate", 0))
    except (TypeError, ValueError):
        return 0


def _fps(rate: str | None) -> float:
    if not rate or rate in ("0/0", "0"):
        return 0.0
    num, _, den = str(rate).partition("/")
    try:
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _video_mime(path: Path) -> str:
    return {".mov": "video/quicktime", ".webm": "video/webm"}.get(path.suffix.lower(), "video/mp4")


def _image_mime(fmt: str) -> str:
    return {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}.get(
        fmt, mimetypes.types_map.get(f".{fmt.lower()}", "application/octet-stream")
    )


def _flatten(img):
    from PIL import Image

    if img.mode in ("RGB", "L"):
        return img.convert("RGB") if img.mode == "L" else img
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    return img.convert("RGB")


def _pad_aspect(img, min_aspect: float | None, max_aspect: float | None):
    """Pad (never crop) to the allowed aspect range, using a blurred copy of
    the image as the background so it looks intentional, not letterboxed."""
    from PIL import ImageFilter, ImageOps

    w, h = img.size
    aspect = w / h
    if min_aspect and aspect < min_aspect:
        size = (round(h * min_aspect), h)
    elif max_aspect and aspect > max_aspect:
        size = (w, round(w / max_aspect))
    else:
        return img
    bg = ImageOps.fit(img, size).filter(ImageFilter.GaussianBlur(radius=max(size) // 40 or 1))
    bg.paste(img, ((size[0] - w) // 2, (size[1] - h) // 2))
    return bg


def _save_jpeg_under(img, out: Path, max_bytes: int) -> None:
    from PIL import Image

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".part.jpg")
    quality = 92
    while True:
        img.save(tmp, "JPEG", quality=quality, optimize=True, progressive=True)
        if tmp.stat().st_size <= max_bytes:
            break
        if quality > 70:
            quality -= 8
        else:
            img = img.resize((int(img.width * 0.85), int(img.height * 0.85)), Image.LANCZOS)
    os.replace(tmp, out)
