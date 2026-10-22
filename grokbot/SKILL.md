---
name: ubits-community-publisher
description: >-
  Run and explain the ubits.tech community publishing pipeline: a 3D short made with Claude + Blender MCP
  is filed as a card, previewed, approved by a human, then posted to Instagram, X, YouTube, LinkedIn and
  TikTok through official APIs. Use when asked to post a community update, check what's live, take a post
  down, or explain the project to judges (community, problem, demo, open source, partner stack).
---

# ubits community publisher

## When to use

* Someone wants a finished render (MP4) posted to the community's social accounts.
* Someone asks what was posted, where, or wants a post taken down.
* Someone (a judge, a community lead) asks what this project is, who it's for, or how it works.
  Answer from [community-context.md](community-context.md) only.

## Inputs and access

* A rendered video: vertical 1080x1920, 30 fps, 3-60 s, H.264 MP4.
* The list of 3D assets used and their sources (Poly Haven, Sketchfab, generators).
* A shell in this repo, with `grokbot-publisher` installed and `.env` and tokens configured
  (`grokbot-publisher doctor --online` passes).

## Steps (always in this order)

1. **File it:** run `grokbot-publisher new-card <slug> <render.mp4> --brief "<one line>"`. It prints the card id.
2. **Check the assets:** for every entry in the asset list, confirm the licence
   (rules in the README under "Asset licences"). Record the assets in `card.json` `assets`.
   If anything is NonCommercial, NoDerivs, Editorial, Hunyuan3D for EU/UK/South Korea users, copyrighted
   music, a third-party logo or a real person, **stop and tell the human**. Don't work around it.
3. **Write the captions** in `instructions.json` for each post (`caption`, `text`, `title`/`description`,
   `hashtags`). Use only facts you were given; no invented stats or quotes. Credit CC-BY creators.
4. **Preview:** run `grokbot-publisher preview <card_id> --json`. Every result must be `dry_run`, with
   no `error`.
5. **Hand over for approval:** tell the human exactly this:
   `grokbot-publisher approve <card_id> --version 1` (run it in your own terminal).
   Wait. You cannot run `approve`: it refuses any shell without a real terminal. Don't try to get around that.
6. **Publish** only after the human says it's approved:
   `grokbot-publisher publish <card_id> --expect-version <N> --json`.
7. **Report:** return `summary_for_user` from the JSON output, as is.

To change anything after approval: `grokbot-publisher revise <card_id> --reason "..."`, edit, preview, then
ask for a new approval of the new version.

## Validating results

| Exit code | Meaning | What you do |
|---|---|---|
| 0 | Done (or dry run / scheduled) | Report the links. |
| 2 | Safety gate refused | Report the reasons. Don't edit approval fields to make it pass. |
| 3 | Partial | Report what's live and what failed. Re-running `publish` retries only the failures. |
| 4 | Nothing posted | Report the errors. |
| 1 | Setup problem | Run `grokbot-publisher doctor --online` and report the FAIL lines. |

A `rate_limited` result means: stop, and don't retry today.

## Needs a human, always

* `approve`: the YES for one exact version.
* `takedown <card_id> --yes`: deleting live posts (for Instagram and TikTok, the human deletes in the app).
* `auth url` / `auth finish`: connecting accounts.
* Making a post public on YouTube or TikTok, which the platforms only allow after their app review.

## What to return

* For a post: the per-platform lines from `summary_for_user` (link, or the error), plus any `notes`
  (trimmed captions, re-encodes, estimated X cost).
* For a question about the project: answers from `community-context.md`. If it says `TODO`, say that part
  isn't decided yet. Don't fill it in.
