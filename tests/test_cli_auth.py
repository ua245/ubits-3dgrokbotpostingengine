import json
from urllib.parse import parse_qs, urlparse

import pytest
import responses

from grokbot_publisher.auth import OAuth, _parse_code
from grokbot_publisher.cli import main, parse_trigger
from grokbot_publisher.errors import PermanentError, PublisherError

from .conftest import CARD_ID, make_card


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # so no real ./.env is picked up
    monkeypatch.setenv("GROKBOT_CABINET", str(tmp_path / "cabinet"))
    monkeypatch.setenv("PUBLISHER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PUBLISHER_TOKENS_FILE", str(tmp_path / "tokens.json"))
    return tmp_path


# ---------------------------------------------------------------- trigger
def test_trigger_accepts_only_which_card_and_version():
    assert parse_trigger('{"action":"publish","card_id":"%s","card_version":3}' % CARD_ID) == (CARD_ID, 3)


@pytest.mark.parametrize("payload", [
    '{"action":"publish","card_id":"a","card_version":3,"caption":"new text"}',  # can't smuggle content
    '{"action":"delete","card_id":"a","card_version":3}',
    '{"action":"publish","card_id":"a","card_version":true}',
    '{"action":"publish","card_id":"a","card_version":"3"}',
    'publish card a please',
    '[]',
])
def test_trigger_rejects_anything_else(payload):
    with pytest.raises(PublisherError):
        parse_trigger(payload)


# -------------------------------------------------------------------- cli
def test_cli_check_exit_codes(env, capsys):
    make_card(env / "cabinet")
    assert main(["check", CARD_ID]) == 0
    assert main(["check", CARD_ID, "--expect-version", "2"]) == 2
    assert "trigger asked for v2" in capsys.readouterr().out


def test_cli_trigger_refused_card_exits_2(env, capsys):
    make_card(env / "cabinet", approved=False)
    payload = json.dumps({"action": "publish", "card_id": CARD_ID, "card_version": 3})
    assert main(["trigger", payload, "--json"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["overall"] == "refused"


def test_cli_init_and_fingerprint(env, capsys):
    assert main(["init"]) == 0
    assert (env / "cabinet" / "approved").is_dir()
    make_card(env / "cabinet")
    assert main(["fingerprint", CARD_ID]) == 0
    assert len(capsys.readouterr().out.strip().splitlines()[-1]) == 64


def test_cli_takedown_requires_yes(env, capsys):
    from grokbot_publisher.config import Settings
    from grokbot_publisher.ledger import Ledger

    Ledger(Settings.from_env().ledger_file).append(
        "published", card_id=CARD_ID, post_key="0:x", platform="x", post_id="1", url="https://x.com/u/status/1")
    assert main(["takedown", CARD_ID]) == 1
    assert "would delete x" in capsys.readouterr().out


def test_cli_auth_set_and_status_never_prints_secrets(env, capsys):
    assert main(["auth", "set", "x", "--access-token", "SECRET", "--username", "ubitstech"]) == 0
    capsys.readouterr()
    assert main(["auth", "status"]) == 0
    out = capsys.readouterr().out
    assert "SECRET" not in out and json.loads(out)["x"]["account"] == "ubitstech"


# ------------------------------------------------------------------- auth
def test_parse_code_variants():
    assert _parse_code("https://ubits.tech/cb?code=abc&state=s1") == ("abc", "s1")
    assert _parse_code("https://ubits.tech/cb?code=abc#_") == ("abc", "")
    assert _parse_code("plaincode#_") == ("plaincode", "")
    with pytest.raises(PermanentError, match="declined"):
        _parse_code("https://ubits.tech/cb?error=access_denied&error_description=no")


def test_oauth_x_pkce_flow(settings, tokens, http):
    oauth = OAuth(settings, tokens, http)
    url = oauth.authorize_url("x")
    q = parse_qs(urlparse(url).query)
    assert q["code_challenge_method"] == ["S256"] and "offline.access" in q["scope"][0]
    state = q["state"][0]

    with pytest.raises(PermanentError, match="state mismatch"):
        oauth.finish("x", "http://127.0.0.1:8765/callback?code=c&state=wrong")

    with responses.RequestsMock() as rs:
        rs.post("https://api.x.com/2/oauth2/token",
                json={"access_token": "AT", "refresh_token": "RT", "expires_in": 7200})
        rs.get("https://api.x.com/2/users/me", json={"data": {"id": "9", "username": "ubitstech"}})
        info = oauth.finish("x", f"http://127.0.0.1:8765/callback?code=c&state={state}")
        body = parse_qs(rs.calls[0].request.body)
        assert body["code_verifier"][0] and body["client_id"] == ["xcid"]
    assert info["account"] == "ubitstech"
    assert tokens.get("x")["refresh_token"] == "RT"
