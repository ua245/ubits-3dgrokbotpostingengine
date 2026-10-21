import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest
import responses

from grokbot_publisher.config import Settings
from grokbot_publisher.errors import ConfigError
from grokbot_publisher.storage import LocalStorage, S3Storage, make_storage, presign_url


def test_sigv4_matches_aws_published_example():
    # https://docs.aws.amazon.com/AmazonS3/latest/API/sigv4-query-string-auth.html
    url = presign_url(
        method="GET", endpoint="https://s3.amazonaws.com", region="us-east-1", bucket="examplebucket",
        key="test.txt", access_key="AKIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", expires=86400, addressing="virtual",
        now=datetime(2013, 5, 24, tzinfo=timezone.utc),
    )
    assert url.endswith("X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404")


def test_path_style_url_for_r2():
    url = presign_url(method="GET", endpoint="https://acct.r2.cloudflarestorage.com", region="auto",
                      bucket="media", key="grokbot-tmp/card/a b.mp4", access_key="k", secret_key="s", expires=60)
    parsed = urlparse(url)
    assert parsed.netloc == "acct.r2.cloudflarestorage.com"
    assert parsed.path == "/media/grokbot-tmp/card/a%20b.mp4"
    assert parse_qs(parsed.query)["X-Amz-Expires"] == ["60"]


def _s3_settings(tmp_path, **extra):
    env = {"GROKBOT_CABINET": str(tmp_path), "S3_ENDPOINT": "https://acct.r2.cloudflarestorage.com",
           "S3_BUCKET": "media", "S3_ACCESS_KEY_ID": "k", "S3_SECRET_ACCESS_KEY": "s", **extra}
    return Settings.from_env(env=env)


@responses.activate
def test_s3_put_uploads_then_returns_expiring_get_link(tmp_path, http):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"video-bytes")
    responses.put(re.compile(r"https://acct\.r2\.cloudflarestorage\.com/media/.*"))
    obj = S3Storage(_s3_settings(tmp_path), http).put(f, "card-1")
    assert responses.calls[0].request.method == "PUT"
    assert responses.calls[0].request.headers["Content-Type"] == "video/mp4"
    assert "X-Amz-Signature=" in obj.url and "X-Amz-Expires=3600" in obj.url
    assert obj.key.startswith("grokbot-tmp/card-1/") and obj.key.endswith("/clip.mp4")


def test_s3_requires_config(tmp_path, http):
    with pytest.raises(ConfigError) as exc:
        make_storage(Settings.from_env(env={"GROKBOT_CABINET": str(tmp_path)}), http)
    assert "S3_BUCKET" in str(exc.value)


def test_local_storage_requires_https(tmp_path):
    s = Settings.from_env(env={"GROKBOT_CABINET": str(tmp_path), "STORAGE_BACKEND": "local",
                               "LOCAL_PUBLIC_DIR": str(tmp_path / "pub"),
                               "LOCAL_PUBLIC_BASE_URL": "http://insecure.example"})
    with pytest.raises(ConfigError):
        LocalStorage(s)


def test_local_storage_copies_and_cleans_up(tmp_path):
    s = Settings.from_env(env={"GROKBOT_CABINET": str(tmp_path), "STORAGE_BACKEND": "local",
                               "LOCAL_PUBLIC_DIR": str(tmp_path / "pub"),
                               "LOCAL_PUBLIC_BASE_URL": "https://tunnel.example/"})
    f = tmp_path / "a.jpg"
    f.write_bytes(b"img")
    st = LocalStorage(s)
    obj = st.put(f, "card-1")
    assert obj.url.startswith("https://tunnel.example/card-1/")
    assert (tmp_path / "pub" / obj.key).read_bytes() == b"img"
    st.delete(obj)
    assert not (tmp_path / "pub" / obj.key).exists()
