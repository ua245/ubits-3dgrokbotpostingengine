# Connecting the platform accounts

Every platform uses the same two-step flow, and it works on a headless machine too:

```bash
grokbot-publisher auth url <platform>        # prints a link: open it on any device and approve
grokbot-publisher auth finish <platform> '<the full URL the browser landed on>'
```

The browser lands on your redirect URI after you approve. It may show an error page, and that's fine:
copy the whole URL from the address bar (it contains `?code=…`) and paste it into `auth finish`. The
redirect URI must be registered in the developer app **exactly** as it appears in `.env`.

If a developer portal gives you a token directly, store it with:

```bash
grokbot-publisher auth set <platform> --access-token '…' [--refresh-token '…'] [--expires-in 5184000] [--user-id …]
```

Then confirm with `grokbot-publisher doctor --online`.

Do the platforms in demo order: **Instagram, X, then the rest.**

---

## Temporary storage (needed for Instagram)

Instagram fetches media from a public URL. Cloudflare R2 is the cheapest option:

1. In Cloudflare, go to **R2**, create a bucket (e.g. `grokbot-media`), then create an **API token** with *Object Read & Write* on that bucket.
2. Set `S3_ENDPOINT=https://<ACCOUNT_ID>.r2.cloudflarestorage.com`, `S3_REGION=auto`, `S3_BUCKET`,
   `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`.
3. Add a lifecycle rule that deletes objects under `grokbot-tmp/` after 1 day. The Publisher deletes each
   file once Instagram has it, so this only catches leftovers from crashes.

The bucket stays private. Instagram gets a presigned link that expires after `S3_URL_EXPIRY` seconds
(default 1 hour), at an unguessable path. `doctor --online` uploads a test file and fetches it through
the link to prove this works.

No bucket? Use `STORAGE_BACKEND=local` with a folder you already serve over **https** (for example through
`cloudflared tunnel`).

## Instagram

Requirements: an Instagram **Business or Creator** account. A Facebook Page isn't needed when you use
Instagram Login.

1. Go to developers.facebook.com, **Create app**, choose the *Instagram* use case, then **API setup with Instagram login**.
2. Add the permissions `instagram_business_basic` and `instagram_business_content_publish`.
3. Add your redirect URI (it must be https, e.g. `https://ubits.tech/oauth/callback`) under *Business login settings*.
4. Add the ubits.tech Instagram account as a tester under *App roles* and accept the invite in the Instagram app.
5. Put the Instagram app ID and secret in `.env` (`INSTAGRAM_APP_ID`, `INSTAGRAM_APP_SECRET`, `INSTAGRAM_REDIRECT_URI`).
6. `grokbot-publisher auth url instagram`, then `auth finish instagram '<url>'`. The Publisher swaps the 1-hour token for a 60-day one and refreshes it during its final week.

While the app is in development mode it can publish to accounts that have a role on the app, which is
enough for the demo.

## X

1. Go to the developer console at console.x.com. Create a project and app, then add billing (pay-per-use).
2. Under *User authentication settings*, enable OAuth 2.0, choose type **Web App / Automated App** (confidential) or **Native App** (public), and set the callback to `http://127.0.0.1:8765/callback`.
3. Set app permissions to **Read and write**.
4. `.env`: `X_CLIENT_ID`, plus `X_CLIENT_SECRET` for a confidential client.
5. `grokbot-publisher auth url x`, then `auth finish x '<url>'`. Scopes requested: `tweet.read tweet.write users.read media.write offline.access`.

Refresh tokens rotate on every refresh, and the Publisher saves the new one immediately under a lock. If you
copy `tokens.json` between machines, use only one copy afterwards, or the other copy's refresh token goes stale.

## YouTube

1. In the Google Cloud console, create a project and enable **YouTube Data API v3**.
2. Under *OAuth consent screen*, use External and add yourself (and the brand channel's owner) as test users.
3. Under *Credentials*, create an OAuth client ID of type **Web application** with redirect `http://127.0.0.1:8765/callback`.
4. `.env`: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`.
5. `grokbot-publisher auth url youtube`, then `auth finish youtube '<url>'`. If the brand channel is
   separate, pick it on Google's account chooser.

Uploads stay private until Google audits the project. While the consent screen is in **Testing**,
refresh tokens expire after **7 days**, so re-authorise shortly before the demo.

## LinkedIn

1. On linkedin.com/developers, create an app linked to the ubits.tech page.
2. Under *Products*, add **Share on LinkedIn** (gives `w_member_social`) and **Sign In with LinkedIn using OpenID Connect** (gives `openid profile`).
3. Under *Auth*, add the redirect URL (https).
4. `.env`: `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET`, `LINKEDIN_REDIRECT_URI`.
5. `grokbot-publisher auth url linkedin`, then `auth finish linkedin '<url>'`.

Posting as the company page needs the **Community Management API** product, which requires a review that can
take weeks. Once approved, set `LINKEDIN_ORG_URN=urn:li:organization:<id>` and use `"as": "organization"`.
Until then that setting fails with a clear message rather than posting to your profile by mistake.

## TikTok

1. On developers.tiktok.com, create an app and add the products **Login Kit** and **Content Posting API**, with *Direct Post* enabled.
2. Scopes: `user.info.basic`, `video.publish`, `video.upload`. Add the https redirect URI.
3. Add the ubits.tech TikTok account as a target user (sandbox) while unaudited.
4. `.env`: `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET`, `TIKTOK_REDIRECT_URI`.
5. `grokbot-publisher auth url tiktok`, then `auth finish tiktok '<url>'`.

Until TikTok audits the app, every direct post is private (`"privacy": "private"`, which the Publisher
sends as `SELF_ONLY`). Access tokens last 24 hours and are refreshed automatically; refresh tokens last a year.

`"mode": "draft"` sends the video to the account's TikTok inbox instead, to finish posting in the app. I
couldn't confirm whether TikTok lets an unaudited app's drafts be posted publicly from the app, so test it
before relying on it for the demo.
