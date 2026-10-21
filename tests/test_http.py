import pytest
import requests
import responses

from grokbot_publisher.errors import AuthExpiredError, PermanentError, RateLimitError, TransientError
from grokbot_publisher.http import Http

URL = "https://api.example.com/thing"


class Auth:
    def __init__(self, can_refresh=True):
        self.token = "old"
        self.refreshes = 0
        self.can_refresh = can_refresh

    def apply(self, headers, params):
        headers["Authorization"] = f"Bearer {self.token}"

    def refresh(self):
        if not self.can_refresh:
            return False
        self.refreshes += 1
        self.token = "new"
        return True


@pytest.fixture
def http():
    sleeps = []
    h = Http(timeout=5, max_retries=3, backoff_base=0.01, sleep=sleeps.append)
    h.sleeps = sleeps
    return h


@responses.activate
def test_transient_get_is_retried_then_succeeds(http):
    responses.get(URL, status=502)
    responses.get(URL, status=500)
    responses.get(URL, json={"ok": True})
    assert http.request("GET", URL, platform="t").json() == {"ok": True}
    assert len(responses.calls) == 3
    assert len(http.sleeps) == 2


@responses.activate
def test_transient_gives_up_after_max_retries(http):
    for _ in range(5):
        responses.get(URL, status=503)
    with pytest.raises(TransientError):
        http.request("GET", URL, platform="t")
    assert len(responses.calls) == 4  # 1 + 3 retries


@responses.activate
def test_rate_limit_is_never_retried(http):
    responses.post(URL, status=429, headers={"Retry-After": "900"})
    with pytest.raises(RateLimitError) as exc:
        http.request("POST", URL, platform="t")
    assert len(responses.calls) == 1
    assert exc.value.retry_after == 900
    assert http.sleeps == []


@responses.activate
def test_non_idempotent_post_not_retried_on_500(http):
    responses.post(URL, status=500)
    with pytest.raises(TransientError) as exc:
        http.request("POST", URL, platform="t", idempotent=False)
    assert len(responses.calls) == 1
    assert "check before re-running" in str(exc.value)


@responses.activate
def test_non_idempotent_post_is_retried_on_503(http):
    responses.post(URL, status=503)
    responses.post(URL, json={"id": "1"})
    assert http.request("POST", URL, platform="t", idempotent=False).json() == {"id": "1"}


@responses.activate
def test_non_idempotent_post_not_retried_after_ambiguous_network_error(http):
    responses.post(URL, body=requests.exceptions.ConnectionError("Connection aborted, RemoteDisconnected"))
    with pytest.raises(TransientError):
        http.request("POST", URL, platform="t", idempotent=False)
    assert len(responses.calls) == 1


@responses.activate
def test_post_retried_when_request_never_left(http):
    responses.post(URL, body=requests.exceptions.ConnectTimeout("connect timeout"))
    responses.post(URL, json={"id": "1"})
    assert http.request("POST", URL, platform="t", idempotent=False).json() == {"id": "1"}


@responses.activate
def test_401_refreshes_once_and_retries_with_new_token(http):
    responses.get(URL, status=401)
    responses.get(URL, json={"ok": True})
    auth = Auth()
    http.request("GET", URL, platform="t", auth=auth)
    assert auth.refreshes == 1
    assert responses.calls[1].request.headers["Authorization"] == "Bearer new"


@responses.activate
def test_401_twice_does_not_loop(http):
    responses.get(URL, status=401)
    responses.get(URL, status=401)
    auth = Auth()
    with pytest.raises(AuthExpiredError):
        http.request("GET", URL, platform="t", auth=auth)
    assert auth.refreshes == 1
    assert len(responses.calls) == 2


@responses.activate
def test_permanent_4xx_fails_fast(http):
    responses.post(URL, status=400, json={"error": "bad"})
    with pytest.raises(PermanentError):
        http.request("POST", URL, platform="t")
    assert len(responses.calls) == 1


@responses.activate
def test_secrets_are_redacted_from_errors(http):
    responses.get(URL, status=400, body='{"access_token=SECRET123"}')
    with pytest.raises(PermanentError) as exc:
        http.request("GET", URL, platform="t", params={"access_token": "SECRET123"})
    assert "SECRET123" not in str(exc.value)


@responses.activate
def test_body_factory_reopens_file_on_retry(http, tmp_path):
    f = tmp_path / "blob.bin"
    f.write_bytes(b"0123456789")
    bodies = []

    def record(request):
        body = request.body.read() if hasattr(request.body, "read") else request.body
        bodies.append(body)
        return (500 if len(bodies) == 1 else 200), {}, ""

    responses.add_callback(responses.PUT, URL, callback=record)
    http.request("PUT", URL, platform="t", body_factory=lambda: f.open("rb"))
    assert bodies == [b"0123456789", b"0123456789"]  # full body sent again, not an exhausted stream
