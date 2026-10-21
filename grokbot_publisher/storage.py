"""Temporary public storage for platforms that pull media from a URL
(Instagram always; TikTok only in PULL_FROM_URL mode).

Backends:
  s3     Any S3-compatible bucket: AWS S3, Cloudflare R2, MinIO. Objects are
         private; the platform gets a presigned GET link that expires.
  local  Copy into a folder you already serve publicly (e.g. via a
         cloudflared tunnel). Handy when you have no bucket.

SigV4 presigning is implemented here (about 40 lines, checked against AWS's
published test vector) so the Switch doesn't need boto3/botocore.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import mimetypes
import secrets
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

from grokbot_publisher.errors import ConfigError
from grokbot_publisher.http import Http

log = logging.getLogger("grokbot_publisher.storage")


@dataclass
class StoredObject:
    key: str
    url: str          # what the platform fetches
    backend: str


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def presign_url(
    *,
    method: str,
    endpoint: str,
    region: str,
    bucket: str,
    key: str,
    access_key: str,
    secret_key: str,
    expires: int,
    addressing: str = "path",
    now: datetime | None = None,
) -> str:
    """AWS Signature Version 4 query-string presigning for S3-compatible APIs."""
    parsed = urlparse(endpoint if "://" in endpoint else f"https://{endpoint}")
    scheme, host = parsed.scheme or "https", parsed.netloc
    encoded_key = quote(key, safe="/-_.~")
    if addressing == "virtual":
        host = f"{bucket}.{host}"
        path = f"/{encoded_key}"
    else:
        path = f"/{bucket}/{encoded_key}"

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    scope = f"{datestamp}/{region}/s3/aws4_request"
    query = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{access_key}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(int(expires)),
        "X-Amz-SignedHeaders": "host",
    }
    canonical_query = "&".join(
        f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in sorted(query.items())
    )
    canonical_request = "\n".join(
        [method.upper(), path, canonical_query, f"host:{host}\n", "host", "UNSIGNED-PAYLOAD"]
    )
    string_to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()]
    )
    k = _sign(("AWS4" + secret_key).encode("utf-8"), datestamp)
    k = _sign(k, region)
    k = _sign(k, "s3")
    k = _sign(k, "aws4_request")
    signature = hmac.new(k, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{scheme}://{host}{path}?{canonical_query}&X-Amz-Signature={signature}"


class S3Storage:
    name = "s3"

    def __init__(self, settings, http: Http):
        missing = [
            n for n in ("s3_endpoint", "s3_bucket", "s3_access_key_id", "s3_secret_access_key")
            if not getattr(settings, n)
        ]
        if missing:
            raise ConfigError(
                "temporary storage not configured: set " + ", ".join(m.upper() for m in missing)
            )
        self.s = settings
        self.http = http

    def _url(self, method: str, key: str, expires: int) -> str:
        return presign_url(
            method=method,
            endpoint=self.s.s3_endpoint,
            region=self.s.s3_region,
            bucket=self.s.s3_bucket,
            key=key,
            access_key=self.s.s3_access_key_id,
            secret_key=self.s.s3_secret_access_key,
            expires=expires,
            addressing=self.s.s3_addressing,
        )

    def put(self, path: Path, card_id: str) -> StoredObject:
        # An unguessable path segment, so the link is private until shared.
        key = f"{self.s.s3_prefix}{card_id}/{secrets.token_urlsafe(12)}/{path.name}"
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.http.request(
            "PUT",
            self._url("PUT", key, 900),
            platform="storage",
            headers={"Content-Type": ctype},
            body_factory=lambda: path.open("rb"),
        )
        url = self._url("GET", key, self.s.s3_url_expiry)
        log.info("storage: uploaded %s (%d bytes), link expires in %ds", path.name, path.stat().st_size,
                 self.s.s3_url_expiry)
        return StoredObject(key=key, url=url, backend=self.name)

    def delete(self, obj: StoredObject) -> None:
        try:
            self.http.request("DELETE", self._url("DELETE", obj.key, 300), platform="storage")
        except Exception as exc:  # cleanup is best effort; the link expires anyway
            log.warning("storage: couldn't delete %s: %s", obj.key, exc)


class LocalStorage:
    name = "local"

    def __init__(self, settings, http: Http | None = None):
        if not settings.local_public_dir or not settings.local_public_base_url:
            raise ConfigError("local storage needs LOCAL_PUBLIC_DIR and LOCAL_PUBLIC_BASE_URL")
        if not settings.local_public_base_url.startswith("https://"):
            raise ConfigError("LOCAL_PUBLIC_BASE_URL must be https:// (platforms refuse plain http)")
        self.root = Path(settings.local_public_dir)
        self.base = settings.local_public_base_url.rstrip("/")

    def put(self, path: Path, card_id: str) -> StoredObject:
        key = f"{card_id}/{secrets.token_urlsafe(12)}/{path.name}"
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        return StoredObject(key=key, url=f"{self.base}/{quote(key)}", backend=self.name)

    def delete(self, obj: StoredObject) -> None:
        target = self.root / obj.key
        try:
            target.unlink()
            target.parent.rmdir()
        except OSError:
            pass


def make_storage(settings, http: Http):
    if settings.storage_backend == "local":
        return LocalStorage(settings, http)
    if settings.storage_backend == "s3":
        return S3Storage(settings, http)
    raise ConfigError(f"unknown STORAGE_BACKEND {settings.storage_backend!r} (use s3 or local)")
