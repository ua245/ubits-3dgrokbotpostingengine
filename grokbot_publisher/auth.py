"""One-time OAuth setup, which also works on a headless machine:

    grokbot-publisher auth url x            # prints a link; open it anywhere
    grokbot-publisher auth finish x '<the URL you were redirected to>'

No local web server is needed. After approving, the browser lands on your
redirect URI (it may show an error page; that's fine). Copy the full URL from
the address bar and paste it into `auth finish`.

Instagram, LinkedIn and TikTok require https redirect URIs: any https URL you
control works (e.g. https://ubits.tech/oauth/callback), as long as it is
registered in the developer app exactly as configured here.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlparse

from grokbot_publisher.config import Settings
from grokbot_publisher.errors import ConfigError, PermanentError
from grokbot_publisher.http import Http, json_body
from grokbot_publisher.tokens import TokenStore
from grokbot_publisher.util import atomic_write_json, read_json


def providers(s: Settings) -> dict:
    return {
        "x": {
            "authorize": "https://x.com/i/oauth2/authorize",
            "token": "https://api.x.com/2/oauth2/token",
            "scopes": ["tweet.read", "tweet.write", "users.read", "media.write", "offline.access"],
            "sep": " ", "pkce": True,
            "client_id": s.x_client_id, "client_secret": s.x_client_secret, "redirect": s.x_redirect_uri,
            "env": "X_CLIENT_ID",
        },
        "youtube": {
            "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
            "token": "https://oauth2.googleapis.com/token",
            "scopes": ["https://www.googleapis.com/auth/youtube.upload",
                       "https://www.googleapis.com/auth/youtube.force-ssl"],
            "sep": " ", "pkce": True, "extra": {"access_type": "offline", "prompt": "consent"},
            "client_id": s.google_client_id, "client_secret": s.google_client_secret,
            "redirect": s.google_redirect_uri, "env": "GOOGLE_CLIENT_ID",
        },
        "linkedin": {
            "authorize": "https://www.linkedin.com/oauth/v2/authorization",
            "token": "https://www.linkedin.com/oauth/v2/accessToken",
            "scopes": ["openid", "profile", "w_member_social"],
            "sep": " ", "pkce": False,
            "client_id": s.linkedin_client_id, "client_secret": s.linkedin_client_secret,
            "redirect": s.linkedin_redirect_uri, "env": "LINKEDIN_CLIENT_ID",
        },
        "tiktok": {
            "authorize": "https://www.tiktok.com/v2/auth/authorize/",
            "token": "https://open.tiktokapis.com/v2/oauth/token/",
            "scopes": ["user.info.basic", "video.publish", "video.upload"],
            "sep": ",", "pkce": False, "client_param": "client_key",
            "client_id": s.tiktok_client_key, "client_secret": s.tiktok_client_secret,
            "redirect": s.tiktok_redirect_uri, "env": "TIKTOK_CLIENT_KEY",
        },
        "instagram": {
            "authorize": "https://www.instagram.com/oauth/authorize",
            "token": "https://api.instagram.com/oauth/access_token",
            "scopes": ["instagram_business_basic", "instagram_business_content_publish"],
            "sep": ",", "pkce": False,
            "client_id": s.instagram_app_id, "client_secret": s.instagram_app_secret,
            "redirect": s.instagram_redirect_uri, "env": "INSTAGRAM_APP_ID",
        },
    }


class OAuth:
    def __init__(self, settings: Settings, tokens: TokenStore, http: Http | None = None):
        self.s = settings
        self.tokens = tokens
        self.http = http or Http.from_settings(settings)
        self.pending_file = settings.state_dir / "oauth_pending.json"

    def _provider(self, platform: str) -> dict:
        p = providers(self.s).get(platform)
        if not p:
            raise ConfigError(f"unknown platform {platform!r}")
        if not p["client_id"] or not p["redirect"]:
            raise ConfigError(f"{platform}: set {p['env']} and the redirect URI in .env first")
        return p

    def _pending(self) -> dict:
        try:
            return read_json(self.pending_file)
        except (FileNotFoundError, ValueError):
            return {}

    def authorize_url(self, platform: str) -> str:
        p = self._provider(platform)
        state = secrets.token_urlsafe(16)
        params = {
            p.get("client_param", "client_id"): p["client_id"],
            "redirect_uri": p["redirect"],
            "response_type": "code",
            "scope": p["sep"].join(p["scopes"]),
            "state": state,
            **p.get("extra", {}),
        }
        pending = {"state": state, "created": time.time()}
        if p["pkce"]:
            verifier = secrets.token_urlsafe(64)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            params.update(code_challenge=challenge, code_challenge_method="S256")
            pending["verifier"] = verifier
        all_pending = self._pending()
        all_pending[platform] = pending
        atomic_write_json(self.pending_file, all_pending, mode=0o600)
        return f"{p['authorize']}?{urlencode(params)}"

    def finish(self, platform: str, redirect_or_code: str) -> dict:
        p = self._provider(platform)
        pending = self._pending().get(platform) or {}
        code, state = _parse_code(redirect_or_code)
        if state and pending.get("state") and state != pending["state"]:
            raise PermanentError("state mismatch: this redirect isn't from the last `auth url` (start again)")
        exchange = getattr(self, f"_exchange_{platform}")
        fields = exchange(p, code, pending)
        entry = self.tokens.update(platform, **fields)
        all_pending = self._pending()
        all_pending.pop(platform, None)
        atomic_write_json(self.pending_file, all_pending, mode=0o600)
        return {"platform": platform, "account": entry.get("username") or entry.get("user_id", ""),
                "expires_at": entry.get("expires_at")}

    def _token_post(self, platform: str, url: str, data: dict, basic=None) -> dict:
        resp = self.http.request("POST", url, platform=platform, idempotent=False, data=data, basic_auth=basic)
        body = json_body(resp)
        if "data" in body and isinstance(body["data"], dict) and "access_token" in body["data"]:
            body = body["data"]
        if not body.get("access_token"):
            raise PermanentError(f"{platform}: token exchange failed: {body}")
        return body

    # -- per platform -----------------------------------------------------
    def _exchange_x(self, p, code, pending):
        data = {"code": code, "grant_type": "authorization_code", "redirect_uri": p["redirect"],
                "code_verifier": pending.get("verifier", "")}
        basic = None
        if p["client_secret"]:
            basic = (p["client_id"], p["client_secret"])
        else:
            data["client_id"] = p["client_id"]
        tok = self._token_post("x", p["token"], data, basic)
        me = json_body(self.http.request("GET", "https://api.x.com/2/users/me", platform="x",
                                         headers={"Authorization": f"Bearer {tok['access_token']}"}))
        user = me.get("data") or {}
        return {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token"),
                "expires_in": tok.get("expires_in"), "username": user.get("username"), "user_id": user.get("id")}

    def _exchange_youtube(self, p, code, pending):
        tok = self._token_post("youtube", p["token"], {
            "code": code, "client_id": p["client_id"], "client_secret": p["client_secret"],
            "redirect_uri": p["redirect"], "grant_type": "authorization_code",
            "code_verifier": pending.get("verifier", ""),
        })
        return {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token"),
                "expires_in": tok.get("expires_in")}

    def _exchange_linkedin(self, p, code, pending):
        tok = self._token_post("linkedin", p["token"], {
            "grant_type": "authorization_code", "code": code, "redirect_uri": p["redirect"],
            "client_id": p["client_id"], "client_secret": p["client_secret"],
        })
        info = json_body(self.http.request("GET", "https://api.linkedin.com/v2/userinfo", platform="linkedin",
                                           headers={"Authorization": f"Bearer {tok['access_token']}"}))
        return {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token"),
                "expires_in": tok.get("expires_in"), "user_id": info.get("sub"), "username": info.get("name")}

    def _exchange_tiktok(self, p, code, pending):
        tok = self._token_post("tiktok", p["token"], {
            "client_key": p["client_id"], "client_secret": p["client_secret"], "code": code,
            "grant_type": "authorization_code", "redirect_uri": p["redirect"],
        })
        return {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token"),
                "expires_in": tok.get("expires_in"), "refresh_expires_in": tok.get("refresh_expires_in"),
                "user_id": tok.get("open_id")}

    def _exchange_instagram(self, p, code, pending):
        short = self._token_post("instagram", p["token"], {
            "client_id": p["client_id"], "client_secret": p["client_secret"], "grant_type": "authorization_code",
            "redirect_uri": p["redirect"], "code": code,
        })
        # Swap the 1-hour token for a 60-day one.
        long = json_body(self.http.request("GET", "https://graph.instagram.com/access_token", platform="instagram",
                                           params={"grant_type": "ig_exchange_token",
                                                   "client_secret": p["client_secret"],
                                                   "access_token": short["access_token"]}))
        token = long.get("access_token") or short["access_token"]
        me = json_body(self.http.request(
            "GET", f"https://graph.instagram.com/{self.s.instagram_api_version}/me", platform="instagram",
            params={"fields": "user_id,username", "access_token": token},
        ))
        return {"access_token": token, "expires_in": long.get("expires_in", 3600),
                "user_id": str(me.get("user_id") or short.get("user_id") or ""), "username": me.get("username")}


def _parse_code(value: str) -> tuple[str, str]:
    """Accept the full redirect URL or just the code. Instagram appends '#_'."""
    value = value.strip()
    if "://" in value or value.startswith("?") or "code=" in value:
        query = parse_qs(urlparse(value if "://" in value else "x://x/?" + value.lstrip("?")).query)
        if "error" in query:
            raise PermanentError(f"authorisation was declined: {query.get('error_description', query['error'])[0]}")
        code = (query.get("code") or [""])[0]
        state = (query.get("state") or [""])[0]
    else:
        code, state = value, ""
    code = code.removesuffix("#_")
    if not code:
        raise PermanentError("no ?code= found in what you pasted")
    return code, state
