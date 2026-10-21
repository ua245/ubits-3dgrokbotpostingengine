"""The Publisher's fixed procedure. Same steps, same order, every time:

  1. lock the card (two runs can never post the same card at once)
  2. safety gate (refuse the whole card on any failed check)
  3. for each post, one platform at a time:
       already posted?   -> skip (ledger is the source of truth; no reposts)
       not due yet?      -> leave scheduled
       prepare           -> validate, fit text, fit media
       publish           -> official API
       record            -> ledger line written the moment a post is live
     any failure is contained to that post; the others still go out
  4. write publish_report.json + one history line in card.json
  5. POST the report to the Orchestrator webhook (if configured)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable

from grokbot_publisher import __version__
from grokbot_publisher.cabinet import CARD, INSTRUCTIONS, Cabinet
from grokbot_publisher.config import Settings
from grokbot_publisher.errors import GateRefused, ManualActionRequired, PublisherError
from grokbot_publisher.gate import check as gate_check
from grokbot_publisher.http import Http
from grokbot_publisher.ledger import Ledger
from grokbot_publisher.media import MediaTool
from grokbot_publisher.models import CardReport, Instructions, Post, PostResult, valid_card_id
from grokbot_publisher.platforms import REGISTRY
from grokbot_publisher.platforms.base import Context
from grokbot_publisher.storage import make_storage
from grokbot_publisher.tokens import TokenStore
from grokbot_publisher.util import LockBusy, file_lock, iso, read_json, redact, utcnow

log = logging.getLogger("grokbot_publisher")


class Publisher:
    def __init__(
        self,
        settings: Settings,
        *,
        http: Http | None = None,
        tokens: TokenStore | None = None,
        media: MediaTool | None = None,
        storage_factory: Callable[[], object] | None = None,
        registry: dict | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.s = settings
        self.cabinet = Cabinet(settings.cabinet)
        self.ledger = Ledger(settings.ledger_file)
        self.http = http or Http.from_settings(settings, sleep=sleep)
        self.tokens = tokens or TokenStore(settings.tokens_file)
        self.media = media or MediaTool(settings)
        self.storage_factory = storage_factory or (lambda: make_storage(settings, self.http))
        self.registry = registry or REGISTRY
        self.sleep = sleep
        self.clock = clock

    # ================================================================ publish
    def publish(
        self,
        card_id: str,
        *,
        expect_version: int | None = None,
        only: set[str] | None = None,
        dry_run: bool = False,
        force_repost: bool = False,
        move_on_success: bool = False,
    ) -> CardReport:
        report = CardReport(card_id=card_id, card_version=None, dry_run=dry_run)
        if not valid_card_id(card_id):
            report.refused_reasons = [f"invalid card_id {card_id!r}"]
            return report.finish()
        try:
            with file_lock(self.s.locks_dir / f"{card_id}.lock"):
                self._run(report, card_id, expect_version, only, dry_run, force_repost)
        except LockBusy as exc:
            report.refused_reasons = [str(exc)]
            report.finish()
            return report  # the other run owns the report file; don't touch it

        report.finish()
        found = self.cabinet.find(card_id)
        if found:
            _, card_dir = found
            if not dry_run:
                self.cabinet.write_report(card_dir, self._report_payload(report))
                self.cabinet.append_history(card_dir, self._history_line(report))
            if move_on_success and report.complete and not dry_run and found[0] == "approved":
                self.cabinet.move(card_id, "approved", "published")
                log.info("moved %s to /published/", card_id)
        if not dry_run:
            self.notify(report)
        return report

    def _run(self, report: CardReport, card_id: str, expect_version, only, dry_run, force_repost) -> None:
        try:
            gp = gate_check(self.cabinet, card_id, expect_version=expect_version,
                            enabled_platforms=self.s.enabled_platforms)
        except GateRefused as exc:
            report.refused_reasons = exc.reasons
            log.warning("gate refused %s: %s", card_id, "; ".join(exc.reasons))
            return
        instr = gp.instructions
        report.card_version = instr.card_version
        log.info("gate passed: %s v%d (%d posts)", card_id, instr.card_version, len(instr.posts))

        ctx_base = dict(settings=self.s, http=self.http, tokens=self.tokens, media=self.media,
                        storage_factory=self.storage_factory, card_id=card_id,
                        ai_generated=instr.ai_generated, sleep=self.sleep, clock=self.clock)
        for post in instr.posts:
            if only and post.platform not in only:
                report.results.append(PostResult(post.index, post.platform, "skipped", notes=["not selected"]))
                continue
            result = self._one(post, instr, Context(**ctx_base), dry_run, force_repost)
            report.results.append(result)
            log.info("%s: %s %s", post.platform, result.status, result.url or result.error or "")

    def _one(self, post: Post, instr: Instructions, ctx: Context, dry_run: bool, force_repost: bool) -> PostResult:
        existing = self.ledger.published_for(instr.card_id, post.key)
        if existing and not force_repost:
            return PostResult(
                post.index, post.platform, "already_published", post_id=str(existing.get("post_id", "")),
                url=existing.get("url", ""), privacy=existing.get("privacy", ""),
                notes=[f"posted {existing.get('ts')} from v{existing.get('card_version')}; not reposting"],
            )
        due = post.due_at()
        if due and due > utcnow() and not dry_run:
            return PostResult(post.index, post.platform, "scheduled", due_at=iso(due))

        platform = self.registry[post.platform](ctx)
        try:
            prepared = platform.prepare(post, post.media_names(instr.media))
            if dry_run:
                return PostResult(post.index, post.platform, "dry_run", privacy=prepared.privacy,
                                  notes=prepared.notes, est_cost_usd=prepared.est_cost_usd,
                                  preview=prepared.preview(), due_at=iso(due) if due else "")
            result = platform.publish(prepared)
        except PublisherError as exc:
            result = PostResult(post.index, post.platform, exc.status, error=redact(str(exc)))
        except Exception as exc:  # a bug in one module must never stop the other platforms
            log.exception("%s: unexpected error", post.platform)
            result = PostResult(post.index, post.platform, "failed", error=redact(f"unexpected: {exc!r}"))

        if result.status == "published":
            self.ledger.append(
                "published", card_id=instr.card_id, card_version=instr.card_version, post_key=post.key,
                platform=post.platform, post_id=result.post_id, url=result.url, privacy=result.privacy,
                extra=result.extra or None,
            )
        elif not dry_run:
            self.ledger.append(
                "failed", card_id=instr.card_id, card_version=instr.card_version, post_key=post.key,
                platform=post.platform, status=result.status, error=result.error,
            )
        return result

    # ============================================================== reporting
    def _report_payload(self, report: CardReport) -> dict:
        return {"publisher_version": __version__, **report.to_dict()}

    @staticmethod
    def _history_line(report: CardReport) -> str:
        if report.refused_reasons:
            return f"refused v{report.card_version}: " + "; ".join(report.refused_reasons)[:300]
        parts = [f"{r.platform}={r.status}" for r in report.results]
        return f"v{report.card_version} {report.overall}: " + ", ".join(parts)

    def notify(self, report: CardReport) -> None:
        url = self.s.report_webhook_url
        if not url:
            return
        body = json.dumps(self._report_payload(report), ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.s.report_webhook_secret:
            sig = hmac.new(self.s.report_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
            headers["X-Grokbot-Signature"] = f"sha256={sig}"
        try:
            self.http.request("POST", url, platform="webhook", idempotent=True, headers=headers, data=body)
        except Exception as exc:
            log.warning("report webhook failed (report is still on disk): %s", exc)

    # =============================================================== takedown
    def takedown(self, card_id: str, platform: str | None = None) -> list[dict]:
        """Delete every live post of a card (or one platform of it)."""
        live = self.ledger.live_posts(card_id, platform)
        results = []
        ctx = Context(settings=self.s, http=self.http, tokens=self.tokens, media=self.media,
                      storage_factory=self.storage_factory, card_id=card_id, sleep=self.sleep, clock=self.clock)
        for entry in live:
            name = entry.get("platform")
            keys = dict(card_id=card_id, card_version=entry.get("card_version"), post_key=entry.get("post_key"),
                        platform=name, post_id=entry.get("post_id"), url=entry.get("url"))
            try:
                message = self.registry[name](ctx).delete(entry)
                self.ledger.append("deleted", **keys)
                results.append({**keys, "status": "deleted", "message": message})
            except ManualActionRequired as exc:
                self.ledger.append("manual_takedown", **keys, error=str(exc))
                results.append({**keys, "status": "manual_action_required", "message": str(exc)})
            except Exception as exc:
                self.ledger.append("delete_failed", **keys, error=redact(str(exc)))
                results.append({**keys, "status": "delete_failed", "message": redact(str(exc))})
        found = self.cabinet.find(card_id) if valid_card_id(card_id) else None
        if found and results:
            summary = ", ".join(f"{r['platform']}={r['status']}" for r in results)
            self.cabinet.append_history(found[1], f"takedown: {summary}")
        return results

    # ================================================================== watch
    def pending_work(self, card_dir: Path) -> bool:
        """Cheap check: is anything on this card due and never attempted at
        this version? Failed / rate-limited posts are NOT retried by the
        watcher; re-run `publish` by hand once the cause is fixed."""
        try:
            raw = read_json(card_dir / INSTRUCTIONS)
            if raw.get("approved") is not True:
                return False
            instr = Instructions.parse(raw)
        except Exception:
            return False
        attempted = {
            e.get("post_key") for e in self.ledger.entries()
            if e.get("card_id") == card_dir.name and e.get("event") in ("published", "failed")
            and (e.get("event") == "published" or e.get("card_version") == instr.card_version)
        }
        for post in instr.posts:
            if post.key in attempted:
                continue
            try:
                due = post.due_at()
            except PublisherError:
                return True  # let the gate report it
            if due is None or due <= utcnow():
                return True
        return False

    def watch(self, *, interval: float | None = None, once: bool = False,
              stop: threading.Event | None = None, move_on_success: bool = False) -> None:
        interval = interval or self.s.watch_interval
        stop = stop or threading.Event()
        refused: dict[str, tuple] = {}
        log.info("watching %s every %ss", self.cabinet.folder("approved"), interval)
        while not stop.is_set():
            for card_dir in self.cabinet.approved_cards():
                if stop.is_set():
                    break
                try:
                    if not self.pending_work(card_dir):
                        continue
                    stamp = _mtimes(card_dir)
                    if refused.get(card_dir.name) == stamp:
                        continue  # same files as last refusal; don't spam reports
                    report = self.publish(card_dir.name, move_on_success=move_on_success)
                    if report.overall == "refused":
                        refused[card_dir.name] = stamp
                    else:
                        refused.pop(card_dir.name, None)
                    log.info("%s: %s\n%s", card_dir.name, report.overall, report.summary_for_user())
                except Exception:
                    log.exception("watch: error on %s (continuing)", card_dir.name)
            if once:
                return
            stop.wait(interval)


def _mtimes(card_dir: Path) -> tuple:
    out = []
    for name in (INSTRUCTIONS, CARD):
        try:
            out.append((card_dir / name).stat().st_mtime_ns)
        except FileNotFoundError:
            out.append(0)
    return tuple(out)
