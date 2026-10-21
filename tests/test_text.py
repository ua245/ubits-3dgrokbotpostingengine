import pytest

from grokbot_publisher.errors import ValidationError
from grokbot_publisher.text import (
    ELLIPSIS,
    compose,
    find_urls,
    linkedin_escape,
    trim_to,
    utf8_len,
    utf16_len,
    x_weighted_len,
    youtube_safe,
)


def test_x_weighted_length_rules():
    assert x_weighted_len("hello") == 5
    assert x_weighted_len("世界") == 4                        # CJK counts double
    assert x_weighted_len("👍") == 2
    assert x_weighted_len("👩‍👩‍👧") == 2          # one ZWJ family emoji = 2
    assert x_weighted_len("👍🏽") == 2                        # skin tone modifier is free
    assert x_weighted_len("see https://ubits.tech/a/very/long/path?x=1") == 4 + 23
    assert x_weighted_len("ubits.tech") == 23                 # bare domains are links too


def test_find_urls_detects_bare_domains_and_ignores_plain_words():
    assert find_urls("visit ubits.tech or https://x.com/ubits") == ["ubits.tech", "https://x.com/ubits"]
    assert find_urls("no links here, version 1.5 and e.g. this") == []


def test_compose_appends_dedupes_and_normalises_hashtags():
    r = compose("Launch day #ubits", ["#ubits", "AI agents", "build_in_public", "AI agents"], limit=2200)
    assert r.text == "Launch day #ubits\n\n#AIagents #build_in_public"
    assert not r.trimmed


def test_compose_respects_max_tags_including_tags_in_body():
    r = compose("Body #one #two", ["three", "four", "five"], limit=2200, max_tags=3)
    assert r.text.endswith("#three")
    assert r.notes and "dropped 2" in r.notes[0]


def test_compose_drops_hashtags_before_touching_the_body():
    body = "x" * 270
    r = compose(body, ["ubits", "ai"], limit=280, counter=x_weighted_len)
    assert r.text == body + "\n\n#ubits"
    assert r.trimmed and "dropped 1" in r.notes[0]


def test_compose_trims_body_at_word_boundary_with_ellipsis():
    body = " ".join(["word"] * 100)
    r = compose(body, ["tag"], limit=50, counter=x_weighted_len)
    assert r.text.endswith(ELLIPSIS)
    assert x_weighted_len(r.text) <= 50
    assert not r.text[:-1].endswith(" ")
    assert "#tag" not in r.text


def test_compose_strict_policy_refuses_instead_of_trimming():
    with pytest.raises(ValidationError):
        compose("y" * 300, [], limit=280, counter=x_weighted_len, policy="strict")


def test_trim_to_never_splits_an_emoji_sequence():
    text = "ab " + "👩‍👩‍👧" * 10
    out = trim_to(text, 12, utf16_len)
    assert utf16_len(out) <= 12
    assert not out.rstrip(ELLIPSIS).endswith("‍")


def test_linkedin_escape_reserved_chars_and_hashtag_templates():
    out = linkedin_escape("Launch (beta) [v2] @team *now* ~ok_ #ubits_tech a#b")
    assert out == (
        "Launch \\(beta\\) \\[v2\\] \\@team \\*now\\* \\~ok\\_ {hashtag|\\#|ubits\\_tech} a\\#b"
    )


def test_youtube_rules():
    assert youtube_safe("<b>hi</b>") == "‹b›hi‹/b›"
    assert utf8_len("é") == 2


def test_dotenv_parser_handles_comments_and_quotes(tmp_path):
    from grokbot_publisher.config import load_dotenv

    f = tmp_path / ".env"
    f.write_text('A=1\nB=   # only a comment\nC=false   # trailing\nD="quoted # kept"\n#E=2\nexport F=x\nA=override\n')
    env = {"F": "real"}
    load_dotenv(f, env)
    assert env == {"A": "1", "B": "", "C": "false", "D": "quoted # kept", "F": "real"}
