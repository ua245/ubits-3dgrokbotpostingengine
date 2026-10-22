"""Card lifecycle: new-card -> preview -> approve (human) -> publish -> revise."""

import json

import pytest

from grokbot_publisher.approval import approve, new_card, revise
from grokbot_publisher.cabinet import Cabinet
from grokbot_publisher.cli import main
from grokbot_publisher.errors import GateRefused, ValidationError
from grokbot_publisher.gate import check


@pytest.fixture
def cab(tmp_path):
    c = Cabinet(tmp_path / "cabinet")
    c.ensure()
    return c


def fill_captions(card_dir, text="Orbiting cubes, rendered in Blender."):
    path = card_dir / "instructions.json"
    raw = json.loads(path.read_text())
    for post in raw["posts"]:
        for field in ("caption", "text", "title", "description"):
            if field in post:
                post[field] = text
    path.write_text(json.dumps(raw))


def test_new_card_lands_in_inbox_unapproved(cab, media_dir):
    d = new_card(cab, "orbiting-cubes", [media_dir / "square.jpg"], platforms=("instagram", "x"))
    assert d.parent.name == "inbox" and d.name.endswith("-orbiting-cubes")
    raw = json.loads((d / "instructions.json").read_text())
    assert raw["approved"] is False and raw["card_version"] == 1 and raw["ai_generated"] is True
    assert [p["platform"] for p in raw["posts"]] == ["instagram", "x"]
    assert (d / "square.jpg").is_file()
    with pytest.raises(GateRefused, match="inbox"):
        check(cab, d.name)                       # can't publish
    check(cab, d.name, preview=True)             # but can be previewed


def test_new_card_rejects_bad_slug_and_missing_media(cab, tmp_path):
    with pytest.raises(ValidationError, match="slug"):
        new_card(cab, "Bad Slug!", [tmp_path / "x.mp4"])
    with pytest.raises(ValidationError, match="not an image or video"):
        new_card(cab, "ok", [tmp_path / "missing.mp4"])


def test_approve_refuses_empty_captions_and_wrong_version(cab, media_dir):
    d = new_card(cab, "cubes", [media_dir / "square.jpg"], platforms=("instagram",))
    with pytest.raises(ValidationError, match="has no text"):
        approve(cab, d.name, 1)
    fill_captions(d)
    with pytest.raises(ValidationError, match="approving v2"):
        approve(cab, d.name, 2)


def test_approve_makes_the_gate_pass_and_locks_content(cab, media_dir):
    d = new_card(cab, "cubes", [media_dir / "square.jpg"], platforms=("instagram",))
    fill_captions(d)
    approved_dir = approve(cab, d.name, 1)
    assert approved_dir.parent.name == "approved"
    gp = check(cab, d.name, expect_version=1)
    assert gp.instructions.content_sha256

    fill_captions(approved_dir, "edited after the YES")
    with pytest.raises(GateRefused, match="content changed after approval"):
        check(cab, d.name)


def test_approve_refuses_if_card_changed_while_being_read(cab, media_dir):
    from grokbot_publisher.gate import fingerprint

    d = new_card(cab, "cubes", [media_dir / "square.jpg"], platforms=("instagram",))
    fill_captions(d)
    shown = fingerprint(d)
    fill_captions(d, "swapped while you were reading")
    with pytest.raises(ValidationError, match="changed after you were shown it"):
        approve(cab, d.name, 1, shown_sha256=shown)


def test_revise_invalidates_the_old_yes(cab, media_dir):
    d = new_card(cab, "cubes", [media_dir / "square.jpg"], platforms=("instagram",))
    fill_captions(d)
    approve(cab, d.name, 1)
    inbox_dir, version = revise(cab, d.name, "shorter caption")
    assert version == 2 and inbox_dir.parent.name == "inbox"
    card = json.loads((inbox_dir / "card.json").read_text())
    assert card["approval"]["status"] == "revise" and card["approval"]["approved_version"] is None
    with pytest.raises(GateRefused):
        check(cab, d.name)
    fill_captions(inbox_dir, "shorter")
    approve(cab, d.name, 2)
    assert check(cab, d.name, expect_version=2).instructions.card_version == 2


def test_cli_approve_refuses_without_a_terminal(tmp_path, monkeypatch, capsys, media_dir):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GROKBOT_CABINET", str(tmp_path / "cabinet"))
    monkeypatch.setenv("PUBLISHER_STATE_DIR", str(tmp_path / "state"))
    assert main(["new-card", "cubes", str(media_dir / "square.jpg"), "--platforms", "x"]) == 0
    card_id = capsys.readouterr().out.strip()
    assert main(["approve", card_id, "--version", "1"]) == 1   # pytest's stdin is not a TTY
    assert "agents can't approve" in capsys.readouterr().err
    raw = json.loads((tmp_path / "cabinet" / "inbox" / card_id / "instructions.json").read_text())
    assert raw["approved"] is False


def test_cli_preview_works_before_approval(tmp_path, monkeypatch, capsys, media_dir):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GROKBOT_CABINET", str(tmp_path / "cabinet"))
    monkeypatch.setenv("PUBLISHER_STATE_DIR", str(tmp_path / "state"))
    main(["new-card", "cubes", str(media_dir / "square.jpg"), "--platforms", "x"])
    card_id = capsys.readouterr().out.strip()
    fill_captions(tmp_path / "cabinet" / "inbox" / card_id, "Cubes in orbit")
    assert main(["preview", card_id, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["overall"] == "dry_run"
    assert out["results"][0]["preview"]["fields"]["text"] == "Cubes in orbit"
