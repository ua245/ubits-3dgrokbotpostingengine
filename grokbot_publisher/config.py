"""Settings come from environment variables, optionally loaded from a .env file.

No third-party dotenv dependency: the parser handles KEY=VALUE lines, comments
and optional quotes, which is all the Publisher needs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ALL_PLATFORMS = ("instagram", "x", "youtube", "linkedin", "tiktok")


def load_dotenv(path: Path, environ: dict | None = None) -> None:
    """Load KEY=VALUE pairs into os.environ without overriding real env vars."""
    environ = os.environ if environ is None else environ
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif value.startswith("#"):
            value = ""  # KEY=   # comment
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        environ.setdefault(key, value)


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, default: int) -> int:
    return int(value) if value not in (None, "") else default


def _float(value: str | None, default: float) -> float:
    return float(value) if value not in (None, "") else default


@dataclass
class Settings:
    cabinet: Path
    state_dir: Path
    tokens_file: Path
    enabled_platforms: tuple[str, ...] = ALL_PLATFORMS

    # behaviour
    caption_policy: str = "trim"           # "trim" or "strict"
    http_timeout: float = 60.0
    max_retries: int = 3
    backoff_base: float = 2.0
    backoff_cap: float = 30.0
    poll_timeout: float = 600.0            # waiting for platform-side processing
    watch_interval: float = 30.0
    report_webhook_url: str = ""
    report_webhook_secret: str = ""
    log_file: str = ""

    # media tools
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    ffmpeg_preset: str = "veryfast"
    ffmpeg_threads: int = 0

    # temporary public storage (S3 / R2 / MinIO, or a local dir behind a tunnel)
    storage_backend: str = "s3"            # "s3" or "local"
    s3_endpoint: str = ""
    s3_region: str = "auto"
    s3_bucket: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_prefix: str = "grokbot-tmp/"
    s3_url_expiry: int = 3600
    s3_addressing: str = "path"            # "path" or "virtual"
    local_public_dir: str = ""
    local_public_base_url: str = ""

    # platform knobs
    instagram_api_host: str = "graph.instagram.com"
    instagram_api_version: str = "v24.0"
    instagram_app_id: str = ""
    instagram_app_secret: str = ""
    instagram_redirect_uri: str = ""

    x_client_id: str = ""
    x_client_secret: str = ""
    x_redirect_uri: str = "http://127.0.0.1:8765/callback"
    x_block_links: bool = False
    x_max_video_seconds: int = 140

    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://127.0.0.1:8765/callback"
    youtube_category_id: str = "28"         # Science & Technology

    linkedin_client_id: str = ""
    linkedin_client_secret: str = ""
    linkedin_redirect_uri: str = ""
    linkedin_api_version: str = "202610"
    linkedin_org_urn: str = ""

    tiktok_client_key: str = ""
    tiktok_client_secret: str = ""
    tiktok_redirect_uri: str = ""
    tiktok_source: str = "FILE_UPLOAD"     # or PULL_FROM_URL (needs a verified domain)

    @property
    def ledger_file(self) -> Path:
        return self.state_dir / "ledger.jsonl"

    @property
    def cache_dir(self) -> Path:
        return self.state_dir / "cache"

    @property
    def locks_dir(self) -> Path:
        return self.state_dir / "locks"

    @classmethod
    def from_env(cls, env: dict | None = None, dotenv: Path | None = None) -> Settings:
        if env is None:
            load_dotenv(dotenv or Path.cwd() / ".env")
            env = dict(os.environ)
        g = env.get

        cabinet = Path(g("GROKBOT_CABINET") or "./grokbot-cabinet").expanduser()
        state_dir = Path(g("PUBLISHER_STATE_DIR") or cabinet / ".publisher").expanduser()
        tokens = Path(
            g("PUBLISHER_TOKENS_FILE") or Path.home() / ".config" / "grokbot-publisher" / "tokens.json"
        ).expanduser()
        enabled = tuple(
            p.strip().lower()
            for p in (g("PUBLISHER_ENABLED_PLATFORMS") or ",".join(ALL_PLATFORMS)).split(",")
            if p.strip()
        )

        return cls(
            cabinet=cabinet,
            state_dir=state_dir,
            tokens_file=tokens,
            enabled_platforms=enabled,
            caption_policy=(g("PUBLISHER_CAPTION_POLICY") or "trim").lower(),
            http_timeout=_float(g("PUBLISHER_HTTP_TIMEOUT"), 60.0),
            max_retries=_int(g("PUBLISHER_MAX_RETRIES"), 3),
            backoff_base=_float(g("PUBLISHER_BACKOFF_BASE"), 2.0),
            backoff_cap=_float(g("PUBLISHER_BACKOFF_CAP"), 30.0),
            poll_timeout=_float(g("PUBLISHER_POLL_TIMEOUT"), 600.0),
            watch_interval=_float(g("PUBLISHER_WATCH_INTERVAL"), 30.0),
            report_webhook_url=g("PUBLISHER_REPORT_WEBHOOK_URL", ""),
            report_webhook_secret=g("PUBLISHER_REPORT_WEBHOOK_SECRET", ""),
            log_file=g("PUBLISHER_LOG_FILE", ""),
            ffmpeg=g("PUBLISHER_FFMPEG") or "ffmpeg",
            ffprobe=g("PUBLISHER_FFPROBE") or "ffprobe",
            ffmpeg_preset=g("PUBLISHER_FFMPEG_PRESET") or "veryfast",
            ffmpeg_threads=_int(g("PUBLISHER_FFMPEG_THREADS"), 0),
            storage_backend=(g("STORAGE_BACKEND") or "s3").lower(),
            s3_endpoint=g("S3_ENDPOINT", ""),
            s3_region=g("S3_REGION") or "auto",
            s3_bucket=g("S3_BUCKET", ""),
            s3_access_key_id=g("S3_ACCESS_KEY_ID", ""),
            s3_secret_access_key=g("S3_SECRET_ACCESS_KEY", ""),
            s3_prefix=g("S3_PREFIX") or "grokbot-tmp/",
            s3_url_expiry=_int(g("S3_URL_EXPIRY"), 3600),
            s3_addressing=(g("S3_ADDRESSING") or "path").lower(),
            local_public_dir=g("LOCAL_PUBLIC_DIR", ""),
            local_public_base_url=g("LOCAL_PUBLIC_BASE_URL", ""),
            instagram_api_host=g("INSTAGRAM_API_HOST") or "graph.instagram.com",
            instagram_api_version=g("INSTAGRAM_API_VERSION") or "v24.0",
            instagram_app_id=g("INSTAGRAM_APP_ID", ""),
            instagram_app_secret=g("INSTAGRAM_APP_SECRET", ""),
            instagram_redirect_uri=g("INSTAGRAM_REDIRECT_URI", ""),
            x_client_id=g("X_CLIENT_ID", ""),
            x_client_secret=g("X_CLIENT_SECRET", ""),
            x_redirect_uri=g("X_REDIRECT_URI") or "http://127.0.0.1:8765/callback",
            x_block_links=_bool(g("X_BLOCK_LINKS")),
            x_max_video_seconds=_int(g("X_MAX_VIDEO_SECONDS"), 140),
            google_client_id=g("GOOGLE_CLIENT_ID", ""),
            google_client_secret=g("GOOGLE_CLIENT_SECRET", ""),
            google_redirect_uri=g("GOOGLE_REDIRECT_URI") or "http://127.0.0.1:8765/callback",
            youtube_category_id=g("YOUTUBE_CATEGORY_ID") or "28",
            linkedin_client_id=g("LINKEDIN_CLIENT_ID", ""),
            linkedin_client_secret=g("LINKEDIN_CLIENT_SECRET", ""),
            linkedin_redirect_uri=g("LINKEDIN_REDIRECT_URI", ""),
            linkedin_api_version=g("LINKEDIN_API_VERSION") or "202610",
            linkedin_org_urn=g("LINKEDIN_ORG_URN", ""),
            tiktok_client_key=g("TIKTOK_CLIENT_KEY", ""),
            tiktok_client_secret=g("TIKTOK_CLIENT_SECRET", ""),
            tiktok_redirect_uri=g("TIKTOK_REDIRECT_URI", ""),
            tiktok_source=(g("TIKTOK_SOURCE") or "FILE_UPLOAD").upper(),
        )
