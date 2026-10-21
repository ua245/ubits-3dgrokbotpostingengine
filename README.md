# Grokbot Publisher

The **Publisher** is the last step of the Grokbot content pipeline for ubits.tech. It is a plain,
rule-following Python program, not an AI. The Orchestrator, or the local LLM on the Switch, only tells
it *which* approved card to post. The Publisher then runs the same steps in the same order every time:

```
watch /approved/ ─► safety gate ─► prepare per platform ─► post (official APIs) ─► report ─► log
   or card_id        refuse on any      resize / re-encode,    one platform at a      publish_report.json,
                     failed check       fit captions           time; failures stay    card history,
                                                               contained               webhook, ledger
```

It runs on a laptop and on a Linux-booted Switch (Python 3.10+, ffmpeg, about 30 MB of dependencies, no boto3).

---

## Quick start

```bash
git clone https://github.com/ua245/grokbot_socialmediapostingsetup && cd grokbot_socialmediapostingsetup
python3 -m venv .venv && . .venv/bin/activate
pip install -e .                       # add ".[dev]" for the test suite
sudo apt install ffmpeg                # or: brew install ffmpeg

cp .env.example .env                   # fill in storage + the platforms you'll use
grokbot-publisher init                 # creates grokbot-cabinet/{inbox,approved,published,...}
grokbot-publisher auth url x           # connect accounts (see docs/platform-setup.md)
grokbot-publisher doctor --online      # checks ffmpeg, tokens, storage link and every API

# try it on the example card without sending anything
./examples/make-demo-media.sh
GROKBOT_CABINET=examples/grokbot-cabinet grokbot-publisher preview 20261021-1849-ubits-launch
```

Post for real:

```bash
grokbot-publisher publish 20261021-1849-ubits-launch --expect-version 3
grokbot-publisher watch                # or leave this running; it also runs scheduled posts
```

---

## The safety gate

Before anything is prepared, uploaded or posted, every check below must pass. If any one fails, the whole
card is refused, nothing goes anywhere, and the reasons are reported back. No flag skips the gate.

| # | Check | Why |
|---|---|---|
| 1 | `card_id` is well-formed and the card is in `/approved/` | Cards in inbox/tasks/deleted can't be posted. A bad id can't be used for path tricks. |
| 2 | `instructions.json` → `"approved"` is the JSON literal `true` | `"true"`, `1` and `"yes"` are all refused. |
| 3 | `instructions.card_id` matches the folder | No mixed-up files. |
| 4 | `card.json` `card_version` equals `instructions.json` `card_version` | Catches a card that was edited after its instructions were written. |
| 5 | `card.json` `approval.status == "approved"` **and** `approval.approved_version == card_version` | Your YES covered *this exact version*. A YES for v2 doesn't cover v3. |
| 6 | `--expect-version` (the version in the YES message) matches, if given | The trigger can't post a different version than the one you approved. |
| 7 | `content_sha256` matches, if present | Detects any caption or media edit made after approval without a version bump. |
| 8 | `approved_at` exists, has a timezone and isn't in the future | |
| 9 | Every platform is known and enabled; every `schedule` has a timezone | |
| 10 | Every media file exists, is non-empty and sits inside the card folder | |

> **Schema addition:** check 5 needs `approval.approved_version` in `card.json`. The schema in the
> brief only has `approval.status`. The Orchestrator or Filing must write the version number that the YES
> referred to. Without it, the card is refused.

## What happens after the gate

For each post, in card order and one platform at a time:

1. **Already posted?** The ledger (`grokbot-cabinet/.publisher/ledger.jsonl`) is checked. A post that's
   already live is never posted again, so re-running after a partial failure only retries what failed.
2. **Due?** A post with a future `schedule` waits. X has no scheduling endpoint, so scheduling is done
   here, by `watch`.
3. **Prepare** (no network). Captions are fitted to each platform's counting rules and hashtags are
   placed at the end. Media is re-encoded only if it's out of spec: compliant files are used untouched,
   and a file that only lacks `faststart` is remuxed in seconds. Encodes are cached by (file, spec), so
   Instagram, TikTok and LinkedIn share one 1080p encode, and a retry doesn't re-encode on the Switch.
4. **Post** through the platform's official API.
5. **Record** a ledger line the moment the post is live, before anything else can go wrong.

Failure handling is the same for every platform:

| Failure | Rule |
|---|---|
| Network blip, 5xx | Retry with exponential backoff and jitter (default 3 times). |
| Request that **creates** a post fails ambiguously | Not retried (it might have gone through). Reported as "check before re-running". |
| Expired or rejected token | Refresh once, then retry once. Rotated refresh tokens (X) are saved immediately. |
| **Rate limit / quota** (429, IG publish cap, YouTube `quotaExceeded`, TikTok daily cap) | **Stop immediately for that platform. Never retried**, not even by `watch`. |
| Anything else | That platform fails with the platform's message; the others still go out. |

Over-long captions are trimmed by default: hashtags are dropped from the end first, then the body is cut
at a word boundary with an ellipsis. Every trim is listed in the report. Set `PUBLISHER_CAPTION_POLICY=strict`
to refuse instead. Run `preview` before asking for the WhatsApp YES, so the text you approve is exactly the
text that will be posted. **Videos are never cut**: a video that's too long for a platform fails validation.

## Reporting back to Filing

Every run writes `publish_report.json` into the card folder, adds one line to `card.json` `history`, and
(if configured) POSTs the report to `PUBLISHER_REPORT_WEBHOOK_URL`, signed with
`X-Grokbot-Signature: sha256=<hmac>`:

```json
{
  "card_id": "20261021-1849-ubits-launch",
  "card_version": 3,
  "overall": "partial",
  "complete": false,
  "summary_for_user": "✅ instagram: https://www.instagram.com/reel/…\n✅ youtube: https://youtube.com/shorts/… (private)\n❌ x: x: HTTP 402: out of API credits…",
  "results": [
    { "platform": "instagram", "status": "published", "post_id": "1790…", "url": "https://www.instagram.com/reel/…", "privacy": "public" },
    { "platform": "x", "status": "failed", "error": "x: HTTP 402: …", "est_cost_usd": 0.015 }
  ]
}
```

`summary_for_user` is ready for the Orchestrator to forward on WhatsApp as is. When `complete` is true,
Filing moves the card to `/published/` (or pass `--move-on-success` to let the Publisher do it).

## Takedown

```bash
grokbot-publisher log --card 20261021-1849-ubits-launch --live   # what's up right now
grokbot-publisher takedown 20261021-1849-ubits-launch --yes      # delete everything from that card
grokbot-publisher takedown 20261021-1849-ubits-launch --platform x --yes
```

API deletion works for **X, YouTube and LinkedIn**. **Instagram** (Instagram Login flavour) and **TikTok**
have no delete endpoint. For those, takedown records a `manual_takedown` entry and prints the link to
delete in the app.

---

## instructions.json

This is your version with the optional fields the Publisher understands added. Everything not listed
here is ignored.

```jsonc
{
  "card_id": "20261021-1849-ubits-launch",
  "card_version": 3,
  "approved": true,
  "approved_at": "2026-10-21T19:02:00Z",
  "content_sha256": "",            // optional: `grokbot-publisher fingerprint <id>` at YES time
  "ai_generated": false,           // sets IG is_ai_generated, TikTok is_aigc, YouTube containsSyntheticMedia
  "media": [ { "file": "launch.mp4", "type": "video", "alt_text": "" } ],
  "posts": [
    { "platform": "instagram", "format": "reel", "caption": "...", "hashtags": ["ubits"], "schedule": "now" },
    { "platform": "youtube", "format": "short", "title": "...", "description": "...", "privacy": "private", "schedule": "now" },
    { "platform": "linkedin", "as": "personal", "text": "...", "schedule": "now" },
    { "platform": "x", "text": "...", "reply_text": "optional self-reply", "schedule": "2026-10-22T09:00:00+01:00" },
    { "platform": "tiktok", "caption": "...", "privacy": "private", "mode": "direct", "schedule": "now" }
  ]
}
```

| Field | Where | Notes |
|---|---|---|
| `hashtags` | any post | Appended at the end, deduplicated against tags already in the text. |
| `media` | any post | List of file names to use for this post only (default: all card media). |
| `schedule` | any post | `"now"` or ISO-8601 **with timezone**. A timestamp without a timezone is refused. |
| `format` | instagram | `image`, `reel`, `carousel` (2-20 items). Inferred from the media if omitted. |
| `share_to_feed` | instagram | Reels only, default `true`. |
| `format`, `title`, `description`, `tags`, `privacy`, `category_id` | youtube | `short` = vertical, 3 min max. Landscape input is padded to 1080x1920, not cropped. |
| `as`, `visibility`, `title` | linkedin | `as`: `personal` (default) or `organization` (needs approval + `LINKEDIN_ORG_URN`). |
| `reply_text` | x | Posted as a reply. **A reply with a link is still billed at the link price.** Putting the link in the bio is the only free option. |
| `privacy`, `mode`, `disable_comment`, `disable_duet`, `disable_stitch`, `brand_organic`, `brand_content` | tiktok | `privacy` defaults to `private`. `mode: "draft"` sends the video to your TikTok inbox to finish in the app. |

---

## Commands

| Command | What it does |
|---|---|
| `publish <card_id> [--expect-version N] [--only instagram,x] [--dry-run] [--json]` | Post one card. |
| `preview <card_id>` | Gate + prepare, send nothing. Shows the final text and media per platform. |
| `trigger '<json>'` | The **only** interface for the local LLM: `{"action":"publish","card_id":"…","card_version":3}`. Any other field is rejected, so the model can't change content. |
| `check <card_id> [--expect-version N]` | Safety gate only. |
| `watch [--interval 30] [--once]` | Poll `/approved/`; post cards never attempted at their version; run due schedules. Failed and rate-limited posts are not retried automatically. |
| `takedown <card_id> [--platform p] --yes` | Delete live posts (see above). |
| `log [--card id] [--live] [--json]` | The post ledger. |
| `fingerprint <card_id>` | Content hash for `content_sha256`. |
| `auth url/finish/set/refresh/status` | Connect accounts; see [docs/platform-setup.md](docs/platform-setup.md). |
| `doctor [--online]` | Checks everything; `--online` also calls each API and fetches a test file through the storage link. |
| `init` | Creates the cabinet folders. |

Exit codes: `0` done (or dry run / scheduled), `1` setup/usage error, `2` gate refused, `3` partial, `4` all failed.

## Running it

* **Laptop or Switch, as a service:** `deploy/grokbot-publisher.service` (systemd, niced, memory-capped so
  encoding doesn't starve the local LLM).
* **Docker:** `docker build -t grokbot-publisher . && docker run --env-file .env --user "$(id -u):$(id -g)" -v "$PWD/grokbot-cabinet:/data/cabinet" -v "$HOME/.config/grokbot-publisher:/data/config" grokbot-publisher`
* **Cron instead of a daemon:** `*/2 * * * * grokbot-publisher watch --once`
* **On the Switch:** L4T Ubuntu Jammy ships Python 3.10, which is supported and tested in CI. 1080p
  re-encodes are slow on the Tegra X1, so give the Switch already-compliant H.264 MP4s where you can
  (`preview` tells you which files need work). Set `PUBLISHER_FFMPEG_PRESET=ultrafast` if encodes are too slow.

Tokens live in `~/.config/grokbot-publisher/tokens.json` (chmod 600), outside the repo and the cabinet.

---

## Platform reality check (verified October 2026)

Details below that differ from the project brief are marked **⚠**.

| Platform | What the Publisher does | Catch |
|---|---|---|
| **Instagram** | Uploads to temp storage → container → poll → `media_publish`, in one run; checks `content_publishing_limit` before creating anything. Deletes the temp file afterwards. | **⚠** Meta's content-publishing page gives two figures, 100 posts/24h in one section and 50 in another. The Publisher reads your live quota instead of assuming either. JPEG only (PNG/HEIC are converted). **⚠** The Instagram Login API can't delete posts; Meta's new media-delete endpoint is for the Facebook Login flavour only. |
| **X** | v2 chunked media upload (`/2/media/upload/initialize` → `append` → `finalize`) → `POST /2/tweets`. Every result shows an estimated cost. | Pay-per-use: about $0.015 per post, about $0.20 with a link. `X_BLOCK_LINKS=true` refuses link posts. Needs the `media.write` scope for media. |
| **YouTube** | Resumable `videos.insert`. A dropped connection resumes from the last confirmed byte, so the video isn't uploaded twice. | Private until Google audits the app. **⚠** Google's revision history says an upload now costs about 100 quota units (was about 1,600), though some Google pages still say 1,600. **⚠** If your OAuth consent screen is in *Testing*, refresh tokens die after 7 days, so re-run `auth url youtube` the day before the demo. |
| **LinkedIn** | Posts API (`LinkedIn-Version: 202610`) with Images/Videos upload; commentary escaped for "little text". | Personal profile works now; the company page needs Community Management API approval. Member tokens last 60 days and can't be refreshed by non-partner apps. |
| **TikTok** | Checks `creator_info` first; refuses a privacy level the account isn't allowed before uploading. Default `FILE_UPLOAD` (chunked PUT, no public link needed). | Private (`SELF_ONLY`) until the app passes TikTok's audit. **⚠** `PULL_FROM_URL` only works from a domain you've verified with TikTok, so an R2/S3 presigned link won't work. That's why video uses `FILE_UPLOAD`. Photo posts require `PULL_FROM_URL`. No delete endpoint. |

**Demo plan.** Instagram and X go live and public. YouTube and TikTok post as private ("goes public once
the platform approves our app"). LinkedIn posts to your personal profile. One platform working end to end
beats five half-working: get `doctor --online` green for Instagram and X first.

**Ayrshare / Upload-Post.** They'd replace `platforms/` with one HTTP call, at the cost of a
subscription and handing account access to a third party. The gate, ledger and reporting would stay the
same. That swap is reasonable if app reviews block you, but it isn't needed for the demo plan above.

## Development

```bash
pip install -e ".[dev]"
pytest -q        # 115 tests: gate, retry policy, media (real ffmpeg), every platform flow (mocked), CLI, OAuth
```

The S3/R2 presigner (`storage.py`) is checked against AWS's published SigV4 test vector in the suite. It
also produced identical URLs to botocore in 18 of 18 cases: R2, AWS virtual-host, custom port; GET/PUT/DELETE;
unicode and special-character keys.
