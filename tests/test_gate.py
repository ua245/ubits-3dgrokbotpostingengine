import json

import pytest

from grokbot_publisher.cabinet import Cabinet
from grokbot_publisher.errors import GateRefused
from grokbot_publisher.gate import check, fingerprint

from .conftest import CARD_ID, make_card


@pytest.fixture
def cab(tmp_path):
    return Cabinet(tmp_path / "cabinet")


def refused(cab, **kw):
    with pytest.raises(GateRefused) as exc:
        check(cab, CARD_ID, **kw)
    return " | ".join(exc.value.reasons)


def test_good_card_passes(cab, media_dir):
    make_card(cab.root, media=[{"file": "a.jpg", "type": "image"}], files={"a.jpg": media_dir / "square.jpg"})
    gp = check(cab, CARD_ID, expect_version=3)
    assert gp.instructions.card_version == 3
    assert gp.instructions.media[0].path.name == "a.jpg"


@pytest.mark.parametrize("value", ["true", 1, "yes", False, None])
def test_approved_must_be_literal_true(cab, value):
    make_card(cab.root, approved=value)
    assert '"approved" is' in refused(cab)


def test_yes_for_older_version_does_not_cover_edited_card(cab):
    make_card(cab.root, version=4, approved_version=3)
    assert "YES was given for v3, but this is v4" in refused(cab)


def test_missing_approved_version_is_refused(cab):
    make_card(cab.root, approved_version=None)
    assert "approved_version is not recorded" in refused(cab)


def test_boolean_approved_version_is_refused(cab):
    make_card(cab.root, version=1, approved_version=True)
    assert "approved_version is not recorded" in refused(cab)


def test_card_json_edited_after_instructions(cab):
    make_card(cab.root, version=3, card_version=4)
    assert "card.json is v4 but instructions.json is v3" in refused(cab)


def test_expect_version_from_trigger(cab):
    make_card(cab.root)
    assert "trigger asked for v2" in refused(cab, expect_version=2)


def test_card_outside_approved_folder(cab):
    make_card(cab.root, folder="inbox")
    assert "card is in /inbox/" in refused(cab)


def test_unknown_card(cab):
    assert "not found" in refused(cab)


def test_invalid_card_id_rejected_before_touching_disk(cab):
    with pytest.raises(GateRefused):
        check(cab, "../../etc")


def test_missing_and_escaping_media(cab):
    make_card(cab.root, media=[{"file": "nope.mp4", "type": "video"}, {"file": "../../x.jpg", "type": "image"}])
    reasons = refused(cab)
    assert "nope.mp4' is missing" in reasons
    assert "outside the card folder" in reasons


def test_approval_status_must_be_approved(cab):
    d = make_card(cab.root)
    card = json.loads((d / "card.json").read_text())
    card["approval"]["status"] = "pending"
    (d / "card.json").write_text(json.dumps(card))
    assert "approval.status is 'pending'" in refused(cab)


def test_content_fingerprint(cab, media_dir):
    d = make_card(cab.root, media=[{"file": "a.jpg", "type": "image"}], files={"a.jpg": media_dir / "square.jpg"})
    digest = fingerprint(d)
    raw = json.loads((d / "instructions.json").read_text())
    raw["content_sha256"] = digest
    (d / "instructions.json").write_text(json.dumps(raw))
    check(cab, CARD_ID)  # matches

    raw["posts"][0]["text"] = "edited after approval"
    (d / "instructions.json").write_text(json.dumps(raw))
    assert "content changed after approval" in refused(cab)


def test_media_bytes_are_part_of_fingerprint(cab, media_dir):
    d = make_card(cab.root, media=[{"file": "a.jpg", "type": "image"}], files={"a.jpg": media_dir / "square.jpg"})
    before = fingerprint(d)
    (d / "a.jpg").write_bytes((d / "a.jpg").read_bytes() + b"\0")
    assert fingerprint(d) != before


def test_future_approval_and_naive_schedule(cab):
    make_card(cab.root, approved_at="2099-01-01T00:00:00Z",
              posts=[{"platform": "x", "text": "hi", "schedule": "2026-10-22T09:00:00"}])
    reasons = refused(cab)
    assert "in the future" in reasons
    assert "no timezone" in reasons


def test_unknown_and_disabled_platforms(cab):
    make_card(cab.root, posts=[{"platform": "myspace", "text": "a"}, {"platform": "tiktok", "caption": "b"}])
    reasons = refused(cab, enabled_platforms=("x", "instagram"))
    assert "unknown platform 'myspace'" in reasons
    assert "tiktok is disabled" in reasons


def test_every_problem_is_reported_at_once(cab):
    make_card(cab.root, approved="true", version=4, approved_version=3)
    reasons = refused(cab, expect_version=2)
    assert reasons.count("|") >= 2
