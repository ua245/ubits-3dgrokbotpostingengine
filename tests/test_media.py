import pytest
from PIL import Image

from grokbot_publisher.errors import ValidationError
from grokbot_publisher.media import MB, ImageSpec, MediaTool, VideoSpec, is_faststart
from grokbot_publisher.models import MediaItem
from grokbot_publisher.platforms.base import STANDARD_1080
from grokbot_publisher.platforms.instagram import IMAGE_SPEC as IG_IMAGE
from grokbot_publisher.platforms.x import VIDEO_SPEC as X_VIDEO
from grokbot_publisher.platforms.youtube import SHORTS_PAD

from .conftest import needs_ffmpeg


@pytest.fixture
def tool(settings):
    return MediaTool(settings)


def item(path, kind):
    return MediaItem(file=path.name, type=kind, path=path)


@needs_ffmpeg
def test_compliant_video_is_used_untouched(tool, media_dir):
    p = tool.prepare_video(item(media_dir / "vertical.mp4", "video"), STANDARD_1080, "c")
    assert p.path == media_dir / "vertical.mp4"
    assert (p.width, p.height) == (1080, 1920)
    assert p.notes == []


@needs_ffmpeg
def test_missing_faststart_is_remuxed_not_reencoded(tool, media_dir):
    assert not is_faststart(media_dir / "slowstart.mp4")
    p = tool.prepare_video(item(media_dir / "slowstart.mp4", "video"), STANDARD_1080, "c")
    assert is_faststart(p.path)
    assert "remuxed" in p.notes[0]


@needs_ffmpeg
def test_oversize_video_is_scaled_into_the_box_and_cached(tool, media_dir):
    src = item(media_dir / "vertical.mp4", "video")
    p = tool.prepare_video(src, X_VIDEO, "c")
    assert (p.width, p.height) == (720, 1280)
    assert is_faststart(p.path)
    mtime = p.path.stat().st_mtime_ns
    again = tool.prepare_video(src, X_VIDEO, "c")
    assert again.path == p.path and again.path.stat().st_mtime_ns == mtime  # cache hit, no re-encode


@needs_ffmpeg
def test_landscape_short_is_padded_to_vertical(tool, media_dir):
    p = tool.prepare_video(item(media_dir / "landscape.mp4", "video"), SHORTS_PAD, "c")
    assert (p.width, p.height) == (1080, 1920)


@needs_ffmpeg
def test_size_cap_forces_a_second_lower_bitrate_pass(tool, media_dir):
    first = tool.prepare_video(item(media_dir / "landscape.mp4", "video"),
                               VideoSpec(name="loose", max_long=1920, max_short=1080, max_bytes=500 * MB,
                                         max_kbps=20000, require_h264=True, min_fps=31), "c")
    cap = int(first.size * 0.6)
    tight = VideoSpec(name="tiny", max_long=1920, max_short=1080, max_bytes=cap, max_kbps=20000, min_fps=31)
    p = tool.prepare_video(item(media_dir / "landscape.mp4", "video"), tight, "c")
    assert p.size <= cap


@needs_ffmpeg
def test_unwatchable_size_cap_is_refused(tool, media_dir):
    tiny = VideoSpec(name="tiny", max_long=1920, max_short=1080, max_bytes=50_000, min_fps=31)
    with pytest.raises(ValidationError, match="watchable bitrate"):
        tool.prepare_video(item(media_dir / "landscape.mp4", "video"), tiny, "c")


def test_png_with_alpha_becomes_jpeg(tool, media_dir):
    p = tool.prepare_image(item(media_dir / "alpha.png", "image"), IG_IMAGE, "c")
    assert p.mime == "image/jpeg"
    with Image.open(p.path) as im:
        assert im.format == "JPEG" and im.mode == "RGB"


def test_wide_image_is_padded_not_cropped_to_instagram_range(tool, media_dir):
    p = tool.prepare_image(item(media_dir / "wide.jpg", "image"), IG_IMAGE, "c")
    assert abs(p.width / p.height - 1.91) < 0.02
    assert max(p.width, p.height) <= 1440


def test_compliant_jpeg_is_untouched(tool, media_dir):
    p = tool.prepare_image(item(media_dir / "square.jpg", "image"), IG_IMAGE, "c")
    assert p.path == media_dir / "square.jpg"


def test_jpeg_quality_drops_to_fit_size_cap(tool, tmp_path):
    import random

    random.seed(1)
    noisy = Image.effect_noise((1400, 1400), 100).convert("RGB")
    src = tmp_path / "noisy.png"
    noisy.save(src)
    spec = ImageSpec(name="small", max_bytes=200_000, max_long=1400)
    p = tool.prepare_image(item(src, "image"), spec, "c")
    assert p.size <= 200_000


def test_unreadable_image_is_a_validation_error(tool, tmp_path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    with pytest.raises(ValidationError):
        tool.prepare_image(item(bad, "image"), IG_IMAGE, "c")


@needs_ffmpeg
def test_image_pillow_cannot_read_is_converted_via_ffmpeg_never_passed_through(tool, tmp_path):
    from grokbot_publisher.platforms.x import IMAGE_SPEC as X_IMAGE

    from .conftest import ffmpeg

    # DPX stands in for iPhone HEIC: ffmpeg decodes it, Pillow can't.
    shot = tmp_path / "shot.dpx"
    ffmpeg("-f", "lavfi", "-i", "color=c=red:s=64x64", "-frames:v", "1", "-f", "image2", str(shot))
    from PIL import UnidentifiedImageError

    with pytest.raises(UnidentifiedImageError):
        Image.open(shot)
    p = tool.prepare_image(item(shot, "image"), X_IMAGE, "c")
    assert p.path != shot and p.mime == "image/jpeg"
    assert "DPX format -> JPEG" in p.notes[0]
    with Image.open(p.path) as im:
        assert im.format == "JPEG" and im.size == (64, 64)


def test_unreadable_by_everything_is_a_clear_error(tool, tmp_path):
    junk = tmp_path / "photo.heic"
    junk.write_bytes(b"junk")
    with pytest.raises(ValidationError, match="can't read image"):
        tool.prepare_image(item(junk, "image"), IG_IMAGE, "c")
