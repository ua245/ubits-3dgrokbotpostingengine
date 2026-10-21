"""The fixed procedure end to end, with fake platform modules."""

import hashlib
import hmac
import json

import pytest
import responses

from grokbot_publisher.config import ALL_PLATFORMS
from grokbot_publisher.errors import ManualActionRequired, PermanentError, RateLimitError
from grokbot_publisher.models import CardReport
from grokbot_publisher.platforms.base import Platform, PreparedPost
from grokbot_publisher.runner import Publisher
from grokbot_publisher.util import file_lock

from .conftest import CARD_ID, make_card

ALL_POSTS = [{"platform": p, "text": f"hello {p}", "schedule": "now"} for p in ALL_PLATFORMS]


class Fake(Platform):
    behaviour: dict = {}
    calls: list = []

    def prepare(self, post, media):
        return PreparedPost(post=post, fields={"text": post.body}, media=[], privacy="public")

    def publish(self, pp):
        Fake.calls.append(self.name)
        outcome = Fake.behaviour.get(self.name)
        if isinstance(outcome, BaseException):
            raise outcome
        return self.result(pp, status="published", post_id=f"{self.name}-id",
                           url=f"https://{self.name}.example/p/1")

    def delete(self, entry):
        if self.name == "tiktok":
            raise ManualActionRequired("tiktok: delete in the app")
        Fake.calls.append(f"delete:{self.name}")
        return "deleted"


@pytest.fixture
def pub(settings, tokens, http):
    Fake.behaviour, Fake.calls = {}, []
    registry = {n: type(f"Fake_{n}", (Fake,), {"name": n}) for n in ALL_PLATFORMS}
    return Publisher(settings, http=http, tokens=tokens, registry=registry, storage_factory=lambda: None,
                     sleep=lambda s: None)


def report_file(settings, folder="approved"):
    return json.loads((settings.cabinet / folder / CARD_ID / "publish_report.json").read_text())


def test_one_platform_failing_does_not_stop_the_others(pub, settings):
    make_card(settings.cabinet, posts=ALL_POSTS)
    Fake.behaviour = {"x": PermanentError("x: HTTP 403: duplicate content")}
    report = pub.publish(CARD_ID)

    assert report.overall == "partial"
    statuses = {r.platform: r.status for r in report.results}
    assert statuses == {"instagram": "published", "x": "failed", "youtube": "published",
                        "linkedin": "published", "tiktok": "published"}
    assert Fake.calls == list(ALL_PLATFORMS)  # one at a time, in card order

    saved = report_file(settings)
    assert saved["overall"] == "partial" and not saved["complete"]
    assert "❌ x: x: HTTP 403" in saved["summary_for_user"]
    history = json.loads((settings.cabinet / "approved" / CARD_ID / "card.json").read_text())["history"]
    assert history[-1].endswith("publisher: v3 partial: instagram=published, x=failed, youtube=published, "
                                "linkedin=published, tiktok=published")


def test_rerun_never_reposts_and_only_retries_the_failure(pub, settings):
    make_card(settings.cabinet, posts=ALL_POSTS)
    Fake.behaviour = {"x": PermanentError("boom")}
    pub.publish(CARD_ID)
    Fake.behaviour, Fake.calls = {}, []

    report = pub.publish(CARD_ID)
    assert Fake.calls == ["x"]
    assert report.overall == "success" and report.complete
    already = [r for r in report.results if r.status == "already_published"]
    assert len(already) == 4 and already[0].url == "https://instagram.example/p/1"


def test_rate_limit_and_unexpected_errors_are_contained(pub, settings):
    make_card(settings.cabinet, posts=ALL_POSTS)
    Fake.behaviour = {"instagram": RateLimitError("instagram: limit"), "youtube": RuntimeError("bug")}
    report = pub.publish(CARD_ID)
    by = {r.platform: r for r in report.results}
    assert by["instagram"].status == "rate_limited"
    assert by["youtube"].status == "failed" and "unexpected" in by["youtube"].error
    assert by["x"].status == "published"


def test_gate_refusal_posts_nothing_and_reports_back(pub, settings):
    make_card(settings.cabinet, posts=ALL_POSTS, version=4, approved_version=3)
    report = pub.publish(CARD_ID)
    assert report.overall == "refused" and Fake.calls == []
    saved = report_file(settings)
    assert saved["overall"] == "refused"
    assert "YES was given for v3" in saved["summary_for_user"]
    assert not list(pub.ledger.entries())


def test_scheduled_posts_wait_and_watch_runs_them_when_due(pub, settings):
    posts = [{"platform": "x", "text": "later", "schedule": "2099-01-01T09:00:00Z"},
             {"platform": "linkedin", "text": "now", "schedule": "now"}]
    d = make_card(settings.cabinet, posts=posts)
    report = pub.publish(CARD_ID)
    assert [r.status for r in report.results] == ["scheduled", "published"]
    assert report.overall == "scheduled" and not report.complete
    assert not pub.pending_work(d)  # nothing due yet

    raw = json.loads((d / "instructions.json").read_text())
    raw["posts"][0]["schedule"] = "2020-01-01T09:00:00+00:00"  # now due
    (d / "instructions.json").write_text(json.dumps(raw))
    assert pub.pending_work(d)
    pub.watch(once=True)
    assert Fake.calls == ["linkedin", "x"]


def test_watch_does_not_retry_failures_automatically(pub, settings):
    d = make_card(settings.cabinet, posts=[{"platform": "x", "text": "a"}])
    Fake.behaviour = {"x": RateLimitError("x: 429")}
    pub.watch(once=True)
    pub.watch(once=True)
    assert Fake.calls == ["x"]  # rate limits are never retried by the watcher
    assert not pub.pending_work(d)


def test_watch_ignores_unapproved_cards(pub, settings):
    make_card(settings.cabinet, approved=False)
    pub.watch(once=True)
    assert Fake.calls == []


def test_dry_run_sends_and_records_nothing(pub, settings):
    make_card(settings.cabinet, posts=ALL_POSTS)
    report = pub.publish(CARD_ID, dry_run=True)
    assert report.overall == "dry_run" and Fake.calls == []
    assert not (settings.cabinet / "approved" / CARD_ID / "publish_report.json").exists()
    assert not list(pub.ledger.entries())


def test_only_selected_platforms(pub, settings):
    make_card(settings.cabinet, posts=ALL_POSTS)
    report = pub.publish(CARD_ID, only={"instagram", "x"})
    assert Fake.calls == ["instagram", "x"]
    assert report.overall == "success" and not report.complete


def test_takedown_deletes_and_flags_manual_ones(pub, settings):
    make_card(settings.cabinet, posts=ALL_POSTS)
    pub.publish(CARD_ID)
    results = {r["platform"]: r["status"] for r in pub.takedown(CARD_ID)}
    assert results["x"] == "deleted" and results["tiktok"] == "manual_action_required"
    live = pub.ledger.live_posts(CARD_ID)
    assert [e["platform"] for e in live] == ["tiktok"]  # still up until deleted by hand
    assert "takedown:" in json.loads((settings.cabinet / "approved" / CARD_ID / "card.json").read_text())["history"][-1]


def test_takedown_then_publish_posts_again(pub, settings):
    make_card(settings.cabinet, posts=[{"platform": "x", "text": "a"}])
    pub.publish(CARD_ID)
    pub.takedown(CARD_ID)
    Fake.calls = []
    pub.publish(CARD_ID)
    assert Fake.calls == ["x"]


def test_move_on_success(pub, settings):
    make_card(settings.cabinet, posts=[{"platform": "x", "text": "a"}])
    pub.publish(CARD_ID, move_on_success=True)
    assert (settings.cabinet / "published" / CARD_ID / "publish_report.json").exists()
    assert not (settings.cabinet / "approved" / CARD_ID).exists()


def test_concurrent_run_is_refused(pub, settings):
    make_card(settings.cabinet, posts=[{"platform": "x", "text": "a"}])
    with file_lock(settings.locks_dir / f"{CARD_ID}.lock"):
        report = pub.publish(CARD_ID)
    assert report.overall == "refused" and "locked" in report.refused_reasons[0]
    assert Fake.calls == []


@responses.activate
def test_report_webhook_is_signed(pub, settings):
    settings.report_webhook_url = "https://orchestrator.example/hook"
    settings.report_webhook_secret = "s3cret"
    responses.post("https://orchestrator.example/hook")
    make_card(settings.cabinet, posts=[{"platform": "x", "text": "a"}])
    pub.publish(CARD_ID)
    call = responses.calls[0].request
    expected = hmac.new(b"s3cret", call.body, hashlib.sha256).hexdigest()
    assert call.headers["X-Grokbot-Signature"] == f"sha256={expected}"
    assert json.loads(call.body)["results"][0]["url"] == "https://x.example/p/1"


def test_summary_for_user_marks_private_posts():
    from grokbot_publisher.models import PostResult

    r = CardReport(card_id=CARD_ID, card_version=3)
    r.results = [PostResult(0, "youtube", "published", url="https://youtube.com/shorts/1", privacy="private")]
    assert r.finish().summary_for_user() == "✅ youtube: https://youtube.com/shorts/1 (private)"
