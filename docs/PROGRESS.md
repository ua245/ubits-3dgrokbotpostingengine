# Grokbot / ubits pipeline — progress log

Working notes for what has been set up and run locally (this PC + Cursor), as of **2026-10-22**.
Secrets (`.env`, tokens) stay local and are gitignored.

## Bot roles (pipeline order)

| Role | Job |
|---|---|
| **Orchestrator** | WhatsApp inbound; owns YES/NO; routes make → copyright → file → publish |
| **Content Writer** | Platform captions (facts only); A/B drafts |
| **Blender Make** | Vertical short + asset/licence list via Blender MCP |
| **Copyright Checker** | Poly Haven CC0 / Sketchfab CC-BY credit; block NC-ND, music, people, logos |
| **Filing** | `new-card`, `instructions.json`, `preview`; never publishes |
| **Publisher** | After human YES: `publish <id> --expect-version N` via official APIs |

Full paste-ready prompts: [`GROKBOT_PROMPTS.md`](../GROKBOT_PROMPTS.md).
Skill for the Publisher Grok Bot: [`grokbot/SKILL.md`](../grokbot/SKILL.md).

## Done on this machine

### Environment

- [x] Blender **5.2** installed
- [x] `uv` installed (winget)
- [x] Cursor MCP: `.cursor/mcp.json` → `uvx mcp-for-blender`
- [x] MCP addon enabled; server on `127.0.0.1:9876` (auto-start)
- [x] Python **3.12** + ffmpeg
- [x] `pip install -e .` in this repo; `.env` from example; `grokbot-publisher init` + cabinet dirs
- [x] Fonts relinked in `bit-crew-film.blend` → `ubits-brand/_source/fonts/{Bricolage,Caveat}-Bold.ttf`
- [ ] Restart Cursor / confirm Blender MCP tools appear in chat when needed
- [ ] Fill `.env` platform tokens + R2/S3; `auth url` / `auth finish` until `doctor --online` is green

### Repo / docs captured

- [x] `GROKBOT_PROMPTS.md` — Orchestrator, Blender Make, Filing, Publisher prompts + local checklist
- [x] `docs/blender/*.beats.json` + `*.analysis.json` — Bit hero + crew-film scene notes
- [x] First Content Writer output for the “ship it” short (see below)

### First media test (`2026-10-22-upload`)

| Item | Status |
|---|---|
| Source brief | Own-upload route; 14s 16:9 crew/mascot pile + “ship it.” neon |
| Vertical render | `media/2026-10-22-upload/render_vertical.mp4` (~7.8 MB) |
| Caption drafts | `media/2026-10-22-upload/drafts.md` (LinkedIn, Instagram, TikTok, YouTube unlisted; **X on hold**) |
| Cabinet card | Not filed yet (`new-card` / `preview` still to run) |
| Copyright | Open: music/SFX licence; confirm Blender provenance for `#blender` |
| Approval / publish | Not started |

Open questions from Content (also in `drafts.md`):

1. Style guide / past posts for voice match  
2. Chiptune + plop SFX licence for IG/TikTok  
3. Whether Filing uses the vertical MP4 or a centre crop of 16:9  
4. Confirm `https://ubits.tech` in YouTube descriptions  

### Auth attempts

- YouTube `auth url` was attempted from the wrong cwd (`.\.venv\Scripts\grokbot-publisher` not found). Re-run from this repo:

```powershell
cd Documents\grokbots_ubits_blender_mcp\grokbot_socialmediapostingsetup
.\.venv\Scripts\grokbot-publisher.exe auth url youtube
```

## Next steps

1. Finish platform auth (`doctor --online`).
2. Filing: `new-card` + captions from chosen draft variants + asset licences → `preview`.
3. Orchestrator / human: WhatsApp or terminal `approve <id> --version N`.
4. Publisher: `publish <id> --expect-version N`.
5. Relay `summary_for_user` back on WhatsApp.
