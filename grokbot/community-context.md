# Community context: briefing for Grok Bot

Grok Bot reads this to answer questions from judges and community leads. **Only say what's written
here.** Anything marked `TODO(owner)` isn't decided yet: say so, and don't fill it in.

Track: **Your own community idea (Weird Stuff).** Judges want to hear four things: name the community,
show the problem, show the build fixing it live, and show that a real community would use it next week.

---

## 1. The community

* **Name:** TODO(owner): which community this is for, e.g. the ubits.tech builder community.
* **Where it lives today:** TODO(owner): WhatsApp group, Discord, LinkedIn, Luma calendar…
* **Size and lead:** TODO(owner): roughly how many members, and the lead who'll be in the room or on a call.
* **Who posts for it:** TODO(owner): who currently does the social posting, and how often.

## 2. What hurts today

TODO(owner): replace these with the lead's own words and numbers.

The pain this build was designed around:

* One update means five separate uploads (Instagram, X, YouTube, LinkedIn, TikTok), and every
  platform has different size, length and format rules.
* Nothing stops a wrong or half-finished post going out publicly, and on a community account,
  mistakes and copyright strikes stick.
* Making something eye-catching (a 3D short, not a text post) takes skills and time most community
  leads don't have.

## 3. What we built (all true today)

* **Make:** Claude + Blender MCP builds and renders a vertical 3D short from a one-line brief.
* **File:** Cursor CLI files the render as a "card", writes the captions and checks asset licences.
* **Approve:** the community lead sees exactly what will be posted on each platform, then types the
  card id in a terminal to say YES. **No AI can approve**: the command refuses to run without a real
  terminal, and a YES only covers that exact version. Any later edit needs a new YES.
* **Publish:** the Publisher, a plain program rather than an AI, posts through each platform's
  **official API**, one platform at a time. If one fails, the others still go out. It never
  double-posts, stops immediately at rate limits, and logs every post id so one command can take it down.
* **Quality:** 123 automated tests; CI runs on every push. Code: this repo.

## 4. Live demo (about 3 minutes)

1. Show the render from Blender: TODO(owner): pick the clip.
2. `grokbot-publisher new-card …` then `preview`: the same video, fitted to each platform, with each
   caption shown as it will post.
3. The lead runs `approve` and types the card id.
4. `publish`: Instagram and X go **live and public**. YouTube and TikTok post as **private**, because
   both platforms only allow public posting after an app review, which takes weeks. LinkedIn posts to a
   personal profile, because company pages need partner approval.
5. Open the live links on screen.
6. The safety beat: edit a caption on the approved card and run `publish` again. The gate refuses with
   "content changed after approval".
7. Optional: `takedown --platform x --yes` removes the X post live.

## 5. Real data

TODO(owner): choose one. Options from the event's free data starter kit that fit a community feed:

* **Luma London events API:** "this week in London tech" as a 3D short. No key needed; undocumented,
  so cache results and keep it to a few calls a minute.
* **Devpost hackathons API:** upcoming London hackathons.
* **GitHub REST API:** milestones for the community's repos (stars, releases).

## 6. Open source

* Licence: **MIT** (see `LICENSE`).
* Repository: TODO(owner): the repo must be **public** before judging. It's private right now.

## 7. Partner stack

* **Grok Bot:** this skill (`grokbot/SKILL.md`). Explains the project and can run the pipeline from a shell.
* **Cursor:** Cursor CLI runs the final stage (file, preview, publish).
* **Not used yet:** Supabase, Cognee, Notion, Zero. Say "not used" if asked. Possible next steps:
  Supabase for the post log and reports, Notion (via its MCP server) as the content calendar, and
  Cognee as memory of past posts for style.

## 8. Integration choices (and why)

* Official platform APIs only. No scraping or browser automation, which break platform terms.
* For approvals over WhatsApp we'd use the **WhatsApp Cloud API**. Baileys and whatsapp-web.js are
  against WhatsApp's terms, so they're not used.
* LinkedIn: we post as the signed-in member. Reading other people's or company data needs partner
  approval, so we don't.

## 9. Straight answers to likely questions

* **"Is the posting real?"** Yes, through official APIs. YouTube and TikTok posts are private until those
  platforms review the app.
* **"What stops the AI posting something bad?"** Only a person can approve, approval covers one exact
  version, and the Publisher refuses anything else. Every post can be found again in the log.
* **"What does it cost?"** X charges per post (about $0.015, or about $0.20 for posts with links). The
  other APIs are free within their quotas. Temporary media storage is Cloudflare R2.
* **"Who will use it next week?"** TODO(owner).
* **"What doesn't work yet?"** Public posting on YouTube and TikTok (needs platform review), posting as
  a LinkedIn company page (needs partner approval), and API deletion on Instagram and TikTok (their APIs
  don't offer it, so it's manual).
