"""Each platform module against mocked official endpoints."""

import json
import re
from urllib.parse import parse_qs

import pytest
import responses

from grokbot_publisher.errors import ManualActionRequired, PermanentError, RateLimitError, ValidationError
from grokbot_publisher.models import MediaItem, Post
from grokbot_publisher.platforms.instagram import Instagram
from grokbot_publisher.platforms.linkedin import LinkedIn
from grokbot_publisher.platforms.tiktok import TikTok, chunk_plan
from grokbot_publisher.platforms.x import X
from grokbot_publisher.platforms.youtube import YouTube

from .conftest import needs_ffmpeg

IG = "https://graph.instagram.com/v24.0"


def video(media_dir, name="vertical.mp4"):
    return MediaItem(file=name, type="video", path=media_dir / name)


def image(media_dir, name="square.jpg", alt=""):
    return MediaItem(file=name, type="image", path=media_dir / name, alt_text=alt)


def form(call):
    return parse_qs(call.request.body if isinstance(call.request.body, str) else call.request.body.decode())


# ---------------------------------------------------------------- Instagram
@needs_ffmpeg
@responses.activate
def test_instagram_reel_container_wait_publish(ctx, media_dir, storage):
    responses.get(f"{IG}/1784/content_publishing_limit",
                  json={"data": [{"quota_usage": 3, "config": {"quota_total": 100}}]})
    responses.post(f"{IG}/1784/media", json={"id": "C1"})
    responses.get(f"{IG}/C1", json={"status_code": "IN_PROGRESS"})
    responses.get(f"{IG}/C1", json={"status_code": "FINISHED"})
    responses.post(f"{IG}/1784/media_publish", json={"id": "M1"})
    responses.get(f"{IG}/M1", json={"permalink": "https://www.instagram.com/reel/abc/"})

    ig = Instagram(ctx)
    post = Post(0, "instagram", {"format": "reel", "caption": "Hook line", "hashtags": ["ubits"]})
    pp = ig.prepare(post, [video(media_dir)])
    result = ig.publish(pp)

    assert result.status == "published"
    assert result.post_id == "M1" and result.url == "https://www.instagram.com/reel/abc/"
    create = form(responses.calls[1])
    assert create["media_type"] == ["REELS"]
    assert create["caption"] == ["Hook line\n\n#ubits"]
    assert create["video_url"][0].startswith("https://bucket.example/")
    assert "access_token=instagram-token" in responses.calls[1].request.url
    publish = responses.calls[4]
    assert "creation_id=C1" in publish.request.url or form(publish).get("creation_id") == ["C1"]
    assert storage.deleted, "temporary media must be removed after publishing"
    assert "4/100" in " ".join(result.notes)


@responses.activate
def test_instagram_stops_before_creating_anything_when_quota_is_used(ctx, media_dir, storage):
    responses.get(f"{IG}/1784/content_publishing_limit",
                  json={"data": [{"quota_usage": 100, "config": {"quota_total": 100}}]})
    ig = Instagram(ctx)
    pp = ig.prepare(Post(0, "instagram", {"caption": "x"}), [image(media_dir)])
    with pytest.raises(RateLimitError):
        ig.publish(pp)
    assert len(responses.calls) == 1 and storage.put_calls == []


@responses.activate
def test_instagram_publish_limit_error_is_rate_limit_not_retry(ctx, media_dir):
    responses.get(f"{IG}/1784/content_publishing_limit", json={"data": [{"quota_usage": 0}]})
    responses.post(f"{IG}/1784/media", json={"id": "C1"})
    responses.get(f"{IG}/C1", json={"status_code": "FINISHED"})
    responses.post(f"{IG}/1784/media_publish", status=400,
                   json={"error": {"message": "limit", "code": 9, "error_subcode": 2207042}})
    ig = Instagram(ctx)
    pp = ig.prepare(Post(0, "instagram", {"caption": "x", "format": "image"}), [image(media_dir, alt="alt text")])
    with pytest.raises(RateLimitError):
        ig.publish(pp)
    assert form(responses.calls[1])["alt_text"] == ["alt text"]
    assert sum(1 for c in responses.calls if "media_publish" in c.request.url) == 1


def test_instagram_validates_format_against_media(ctx, media_dir):
    ig = Instagram(ctx)
    with pytest.raises(ValidationError):
        ig.prepare(Post(0, "instagram", {"format": "reel", "caption": "x"}), [image(media_dir)])
    with pytest.raises(ValidationError):
        ig.prepare(Post(0, "instagram", {"format": "carousel", "caption": "x"}), [image(media_dir)])


def test_instagram_login_delete_is_manual(ctx):
    with pytest.raises(ManualActionRequired):
        Instagram(ctx).delete({"post_id": "M1", "url": "https://instagram.com/p/x"})


# ------------------------------------------------------------------------ X
@needs_ffmpeg
@responses.activate
def test_x_video_upload_then_post(ctx, media_dir):
    api = "https://api.x.com/2"
    responses.post(f"{api}/media/upload/initialize", json={"data": {"id": "77", "media_key": "7_77"}})
    responses.post(f"{api}/media/upload/77/append", status=204)
    responses.post(f"{api}/media/upload/77/finalize",
                   json={"data": {"id": "77", "processing_info": {"state": "pending", "check_after_secs": 1}}})
    responses.get(f"{api}/media/upload", json={"data": {"processing_info": {"state": "in_progress"}}})
    responses.get(f"{api}/media/upload", json={"data": {"processing_info": {"state": "succeeded"}}})
    responses.post(f"{api}/tweets", json={"data": {"id": "1900", "text": "hi"}})

    x = X(ctx)
    pp = x.prepare(Post(0, "x", {"text": "Small agents.", "hashtags": ["ubits"]}), [video(media_dir)])
    assert pp.est_cost_usd == 0.015
    result = x.publish(pp)

    assert result.url == "https://x.com/ubitstech/status/1900"
    init = json.loads(responses.calls[0].request.body)
    assert init == {"media_type": "video/mp4", "total_bytes": pp.media[0].size, "media_category": "tweet_video"}
    tweet = json.loads(responses.calls[-1].request.body)
    assert tweet == {"text": "Small agents.\n\n#ubits", "media": {"media_ids": ["77"]}}


@responses.activate
def test_x_rate_limit_posts_exactly_once(ctx):
    responses.post("https://api.x.com/2/tweets", status=429, headers={"x-rate-limit-reset": "0"})
    x = X(ctx)
    with pytest.raises(RateLimitError):
        x.publish(x.prepare(Post(0, "x", {"text": "hi"}), []))
    assert len(responses.calls) == 1


@responses.activate
def test_x_expired_token_is_refreshed_and_rotated_refresh_token_saved(ctx):
    responses.post("https://api.x.com/2/tweets", status=401, json={"title": "Unauthorized"})
    responses.post("https://api.x.com/2/oauth2/token",
                   json={"access_token": "fresh", "refresh_token": "rotated", "expires_in": 7200})
    responses.post("https://api.x.com/2/tweets", json={"data": {"id": "5"}})
    x = X(ctx)
    x.publish(x.prepare(Post(0, "x", {"text": "hi"}), []))
    assert responses.calls[2].request.headers["Authorization"] == "Bearer fresh"
    assert form(responses.calls[1])["client_id"] == ["xcid"]
    saved = ctx.tokens.get("x")
    assert saved["access_token"] == "fresh" and saved["refresh_token"] == "rotated"


def test_x_link_cost_and_block(ctx, settings):
    x = X(ctx)
    pp = x.prepare(Post(0, "x", {"text": "Try ubits.tech", "reply_text": "more soon"}), [])
    assert pp.est_cost_usd == pytest.approx(0.215)
    settings.x_block_links = True
    with pytest.raises(ValidationError, match="bio"):
        x.prepare(Post(0, "x", {"text": "Try https://ubits.tech"}), [])


@responses.activate
def test_x_reply_failure_does_not_fail_the_live_post(ctx):
    responses.post("https://api.x.com/2/tweets", json={"data": {"id": "1"}})
    responses.post("https://api.x.com/2/tweets", status=403, json={"detail": "nope"})
    x = X(ctx)
    result = x.publish(x.prepare(Post(0, "x", {"text": "main", "reply_text": "reply"}), []))
    assert result.status == "published" and "reply failed" in " ".join(result.notes)


def test_x_rejects_mixed_media(ctx, media_dir):
    with pytest.raises(ValidationError):
        X(ctx).prepare(Post(0, "x", {"text": "a"}), [image(media_dir), video(media_dir)])


# ------------------------------------------------------------------ YouTube
@needs_ffmpeg
@responses.activate
def test_youtube_resumable_upload_resumes_after_failure(ctx, media_dir):
    session = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=S1"
    responses.post(re.compile(r"https://www\.googleapis\.com/upload/youtube/v3/videos.*"),
                   headers={"Location": session})
    responses.put(session, status=503)                                   # chunk lost
    responses.put(session, status=308, headers={"Range": "bytes=0-99"})  # status query: has 100 bytes
    responses.put(session, json={"id": "VID1", "status": {"privacyStatus": "private"}})

    yt = YouTube(ctx)
    pp = yt.prepare(Post(0, "youtube", {"format": "short", "title": "T", "description": "D",
                                        "hashtags": ["Shorts"]}), [video(media_dir)])
    result = yt.publish(pp)

    assert result.url == "https://youtube.com/shorts/VID1" and result.privacy == "private"
    meta = json.loads(responses.calls[0].request.body)
    assert meta["status"]["privacyStatus"] == "private"
    assert meta["snippet"]["description"] == "D\n\n#Shorts"
    assert responses.calls[2].request.headers["Content-Range"] == f"bytes */{pp.media[0].size}"
    assert responses.calls[3].request.headers["Content-Range"].startswith("bytes 100-")


@needs_ffmpeg
@responses.activate
def test_youtube_quota_exceeded_is_rate_limit(ctx, media_dir):
    responses.post(re.compile(r"https://www\.googleapis\.com/upload/youtube/v3/videos.*"), status=403,
                   json={"error": {"message": "quota", "errors": [{"reason": "quotaExceeded"}]}})
    yt = YouTube(ctx)
    pp = yt.prepare(Post(0, "youtube", {"title": "T"}), [video(media_dir)])
    with pytest.raises(RateLimitError):
        yt.publish(pp)
    assert len(responses.calls) == 1


@needs_ffmpeg
def test_youtube_short_rules(ctx, media_dir):
    yt = YouTube(ctx)
    pp = yt.prepare(Post(0, "youtube", {"format": "short", "title": "<b>" + "x" * 120}),
                    [video(media_dir, "landscape.mp4")])
    assert len(pp.fields["title"]) <= 100 and "<" not in pp.fields["title"]
    assert (pp.media[0].width, pp.media[0].height) == (1080, 1920)  # padded, not cropped


# ----------------------------------------------------------------- LinkedIn
@responses.activate
def test_linkedin_text_post_escapes_commentary(ctx):
    responses.post("https://api.linkedin.com/rest/posts", status=201,
                   headers={"x-restli-id": "urn:li:share:123"})
    li = LinkedIn(ctx)
    pp = li.prepare(Post(0, "linkedin", {"text": "Agents (ubits) ship", "hashtags": ["AI"]}), [])
    result = li.publish(pp)
    body = json.loads(responses.calls[0].request.body)
    assert body["author"] == "urn:li:person:abc123"
    assert body["commentary"] == "Agents \\(ubits\\) ship\n\n{hashtag|\\#|AI}"
    assert responses.calls[0].request.headers["LinkedIn-Version"] == "202610"
    assert result.url == "https://www.linkedin.com/feed/update/urn:li:share:123/"


@responses.activate
def test_linkedin_image_upload(ctx, media_dir):
    responses.post("https://api.linkedin.com/rest/images",
                   json={"value": {"uploadUrl": "https://upload.linkedin.example/img", "image": "urn:li:image:9"}})
    responses.put("https://upload.linkedin.example/img", status=201)
    responses.post("https://api.linkedin.com/rest/posts", status=201, headers={"x-restli-id": "urn:li:share:1"})
    li = LinkedIn(ctx)
    li.publish(li.prepare(Post(0, "linkedin", {"text": "pic"}), [image(media_dir, alt="a chart")]))
    body = json.loads(responses.calls[2].request.body)
    assert body["content"] == {"media": {"id": "urn:li:image:9", "altText": "a chart"}}


def test_linkedin_company_page_needs_approval(ctx):
    with pytest.raises(ValidationError, match="Community Management"):
        LinkedIn(ctx).prepare(Post(0, "linkedin", {"text": "x", "as": "organization"}), [])


@responses.activate
def test_linkedin_delete(ctx):
    responses.delete("https://api.linkedin.com/rest/posts/urn%3Ali%3Ashare%3A1", status=204)
    LinkedIn(ctx).delete({"post_id": "urn:li:share:1"})
    assert responses.calls[0].request.headers["X-RestLi-Method"] == "DELETE"


# ------------------------------------------------------------------- TikTok
TT = "https://open.tiktokapis.com/v2"


def tt_ok(data):
    return {"data": data, "error": {"code": "ok", "message": ""}}


@needs_ffmpeg
@responses.activate
def test_tiktok_private_file_upload(ctx, media_dir):
    responses.post(f"{TT}/post/publish/creator_info/query/", json=tt_ok(
        {"creator_username": "ubitstech", "privacy_level_options": ["SELF_ONLY"],
         "max_video_post_duration_sec": 600}))
    responses.post(f"{TT}/post/publish/video/init/",
                   json=tt_ok({"publish_id": "P1", "upload_url": "https://upload.tiktok.example/u"}))
    responses.put("https://upload.tiktok.example/u", status=201)
    responses.post(f"{TT}/post/publish/status/fetch/", json=tt_ok({"status": "PROCESSING_UPLOAD"}))
    responses.post(f"{TT}/post/publish/status/fetch/", json=tt_ok({"status": "PUBLISH_COMPLETE"}))

    tt = TikTok(ctx)
    pp = tt.prepare(Post(0, "tiktok", {"caption": "Watch", "privacy": "private"}), [video(media_dir)])
    result = tt.publish(pp)

    init = json.loads(responses.calls[1].request.body)
    size = pp.media[0].size
    assert init["post_info"]["privacy_level"] == "SELF_ONLY"
    assert init["source_info"] == {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": size,
                                   "total_chunk_count": 1}
    assert responses.calls[2].request.headers["Content-Range"] == f"bytes 0-{size - 1}/{size}"
    assert result.privacy == "private" and result.url == "https://www.tiktok.com/@ubitstech"
    assert "private post" in " ".join(result.notes)


@needs_ffmpeg
@responses.activate
def test_tiktok_refuses_public_for_unaudited_app_before_uploading(ctx, media_dir):
    responses.post(f"{TT}/post/publish/creator_info/query/",
                   json=tt_ok({"creator_username": "u", "privacy_level_options": ["SELF_ONLY"]}))
    tt = TikTok(ctx)
    pp = tt.prepare(Post(0, "tiktok", {"caption": "x", "privacy": "public"}), [video(media_dir)])
    with pytest.raises(PermanentError, match="Unaudited"):
        tt.publish(pp)
    assert len(responses.calls) == 1


@responses.activate
def test_tiktok_daily_cap_is_rate_limit(ctx):
    from grokbot_publisher.platforms.tiktok import classify

    responses.post(f"{TT}/x", status=403, json={"error": {"code": "spam_risk_too_many_posts", "message": "cap"}})
    import requests

    resp = requests.post(f"{TT}/x")
    assert isinstance(classify(resp, "tiktok"), RateLimitError)


def test_tiktok_chunk_plan_follows_media_transfer_rules():
    mb = 1024 * 1024
    assert chunk_plan(3 * mb) == (3 * mb, 1)          # under 5 MB: whole file
    assert chunk_plan(64 * mb) == (64 * mb, 1)
    size, count = chunk_plan(105 * mb)
    assert (size, count) == (10 * mb, 10)             # last chunk carries the extra 5 MB
    assert 105 * mb - size * (count - 1) <= 128 * mb


def test_tiktok_delete_is_manual(ctx):
    with pytest.raises(ManualActionRequired):
        TikTok(ctx).delete({"post_id": "1"})
