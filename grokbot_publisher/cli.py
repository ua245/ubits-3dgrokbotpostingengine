"""Command line interface.

Exit codes (stable, so Cursor CLI or any other caller can branch on them):
    0  everything due was posted (or dry run / check passed / scheduled)
    1  usage or setup error
    2  safety gate refused the card (nothing was posted)
    3  partial: some platforms posted, some failed
    4  nothing posted: every attempted platform failed
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import signal
import sys
import threading
import time
from pathlib import Path

from grokbot_publisher import __version__, approval
from grokbot_publisher.auth import OAuth
from grokbot_publisher.cabinet import Cabinet
from grokbot_publisher.config import ALL_PLATFORMS, Settings, load_dotenv
from grokbot_publisher.errors import GateRefused, PublisherError
from grokbot_publisher.gate import check as gate_check
from grokbot_publisher.gate import fingerprint
from grokbot_publisher.models import valid_card_id
from grokbot_publisher.runner import Publisher
from grokbot_publisher.util import iso

EXIT = {"success": 0, "scheduled": 0, "dry_run": 0, "skipped": 0, "refused": 2, "partial": 3, "failed": 4}
TRIGGER_KEYS = {"action", "card_id", "card_version"}


class UTCFormatter(logging.Formatter):
    converter = time.gmtime

    def formatTime(self, record, datefmt=None):  # noqa: N802
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", self.converter(record.created))


def setup_logging(verbosity: int, log_file: str) -> None:
    level = logging.WARNING if verbosity < 0 else logging.DEBUG if verbosity > 0 else logging.INFO
    fmt = UTCFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    err = logging.StreamHandler(sys.stderr)
    err.setFormatter(fmt)
    root.addHandler(err)
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _platforms(value: str) -> set[str]:
    names = {p.strip().lower() for p in value.split(",") if p.strip()}
    unknown = names - set(ALL_PLATFORMS)
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown platform(s): {', '.join(sorted(unknown))}")
    return names


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="grokbot-publisher", description="Post approved Grokbot cards.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--env", type=Path, default=None, help="path to .env (default: ./.env)")
    ap.add_argument("-v", "--verbose", action="count", default=0)
    ap.add_argument("-q", "--quiet", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("publish", help="post one approved card")
    p.add_argument("card_id")
    p.add_argument("--expect-version", type=int, help="the card version the YES was for")
    p.add_argument("--only", type=_platforms, help="comma-separated platforms, e.g. instagram,x")
    p.add_argument("--dry-run", action="store_true", help="gate + prepare only; send nothing")
    p.add_argument("--force-repost", action="store_true",
                   help="post again even if the ledger says it's live (only after deleting it by hand)")
    p.add_argument("--move-on-success", action="store_true", help="move the card to /published/ when done")
    p.add_argument("--json", action="store_true", help="print the full report as JSON")

    p = sub.add_parser("preview", help="show exactly what would be posted (no network)")
    p.add_argument("card_id")
    p.add_argument("--expect-version", type=int)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("trigger", help="strict JSON trigger for agents: "
                                       '{"action":"publish","card_id":"...","card_version":3}')
    p.add_argument("payload", nargs="?", default="-", help="JSON string, or - for stdin")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("check", help="run the safety gate only")
    p.add_argument("card_id")
    p.add_argument("--expect-version", type=int)

    p = sub.add_parser("watch", help="watch /approved/ and post new cards (also runs schedules)")
    p.add_argument("--interval", type=float)
    p.add_argument("--once", action="store_true", help="one pass, then exit (for cron)")
    p.add_argument("--move-on-success", action="store_true")

    p = sub.add_parser("takedown", help="delete a card's live posts")
    p.add_argument("card_id")
    p.add_argument("--platform", choices=ALL_PLATFORMS)
    p.add_argument("--yes", action="store_true", help="required: confirms the deletion")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("log", help="show the post ledger")
    p.add_argument("--card")
    p.add_argument("--platform", choices=ALL_PLATFORMS)
    p.add_argument("--live", action="store_true", help="only posts that are still up")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("fingerprint", help="content hash to store as content_sha256 at approval time")
    p.add_argument("card_id")

    sub.add_parser("init", help="create the cabinet folders")

    p = sub.add_parser("new-card", help="file a rendered video/image into /inbox/ as a new v1 card")
    p.add_argument("slug", help="short-hyphenated-name, e.g. orbiting-cubes")
    p.add_argument("media", nargs="+", type=Path, help="rendered video (or images)")
    p.add_argument("--platforms", type=_platforms, help="default: all five")
    p.add_argument("--brief", default="", help="one line on what the video is")
    p.add_argument("--no-ai-generated", action="store_true",
                   help="don't set the AI-content disclosure flags (default: set them)")

    p = sub.add_parser("approve", help="YOU approve one exact card version (refuses to run for an agent)")
    p.add_argument("card_id")
    p.add_argument("--version", type=int, required=True, help="the version you're saying YES to")

    p = sub.add_parser("revise", help="reopen a card for edits: bump version, clear approval, back to /inbox/")
    p.add_argument("card_id")
    p.add_argument("--reason", default="")

    p = sub.add_parser("doctor", help="check ffmpeg, tokens, storage and API access")
    p.add_argument("--online", action="store_true", help="also call each API and test the storage link")

    auth = sub.add_parser("auth", help="connect platform accounts").add_subparsers(dest="auth_cmd", required=True)
    a = auth.add_parser("url", help="print the authorisation link")
    a.add_argument("platform", choices=ALL_PLATFORMS)
    a = auth.add_parser("finish", help="paste the redirect URL (or code) back")
    a.add_argument("platform", choices=ALL_PLATFORMS)
    a.add_argument("redirect")
    a = auth.add_parser("set", help="store a token you generated elsewhere (e.g. a developer portal)")
    a.add_argument("platform", choices=ALL_PLATFORMS)
    a.add_argument("--access-token", required=True)
    a.add_argument("--refresh-token")
    a.add_argument("--expires-in", type=int, help="seconds until the access token expires")
    a.add_argument("--user-id")
    a.add_argument("--username")
    a = auth.add_parser("refresh", help="refresh a token now")
    a.add_argument("platform", choices=ALL_PLATFORMS)
    auth.add_parser("status", help="which accounts are connected (no secrets shown)")
    return ap


def _print_report(report, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"publisher_version": __version__, **report.to_dict()}, indent=2, ensure_ascii=False))
        return
    print(f"{report.card_id} v{report.card_version}: {report.overall}")
    print(report.summary_for_user())
    for r in report.results:
        for note in r.notes:
            print(f"   - {r.platform}: {note}")
        if r.est_cost_usd:
            print(f"   - {r.platform}: estimated cost ${r.est_cost_usd:.3f}")
        if r.preview:
            print(f"   - {r.platform} preview: {json.dumps(r.preview, ensure_ascii=False, indent=2)}")


def parse_trigger(text: str) -> tuple[str, int]:
    """An agent may only say *which* approved card to post. Anything
    else in the payload is rejected rather than ignored."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise PublisherError(f"trigger is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PublisherError("trigger must be a JSON object")
    extra = set(data) - TRIGGER_KEYS
    if extra:
        raise PublisherError(f"trigger has unexpected fields: {', '.join(sorted(extra))}")
    if data.get("action") != "publish":
        raise PublisherError('trigger action must be "publish"')
    version = data.get("card_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise PublisherError("trigger card_version must be an integer")
    card_id = data.get("card_id")
    if not isinstance(card_id, str):
        raise PublisherError("trigger card_id must be a string")
    return card_id, version


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.env:
        load_dotenv(args.env)
    settings = Settings.from_env()
    setup_logging(-1 if args.quiet else args.verbose, settings.log_file)
    try:
        return dispatch(args, settings)
    except PublisherError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def dispatch(args, settings: Settings) -> int:
    pub = Publisher(settings)

    if args.cmd in ("publish", "preview", "trigger"):
        if args.cmd == "trigger":
            text = sys.stdin.read() if args.payload == "-" else args.payload
            card_id, version = parse_trigger(text)
            report = pub.publish(card_id, expect_version=version)
        elif args.cmd == "preview":
            report = pub.publish(args.card_id, expect_version=args.expect_version, dry_run=True)
        else:
            report = pub.publish(args.card_id, expect_version=args.expect_version, only=args.only,
                                 dry_run=args.dry_run, force_repost=args.force_repost,
                                 move_on_success=args.move_on_success)
        _print_report(report, args.json)
        return EXIT.get(report.overall, 1)

    if args.cmd == "check":
        try:
            gp = gate_check(pub.cabinet, args.card_id, expect_version=args.expect_version,
                            enabled_platforms=settings.enabled_platforms)
        except GateRefused as exc:
            print("REFUSED")
            for reason in exc.reasons:
                print(f" - {reason}")
            return 2
        print(f"PASS: {args.card_id} v{gp.instructions.card_version}, "
              f"{len(gp.instructions.posts)} post(s): {', '.join(p.platform for p in gp.instructions.posts)}")
        return 0

    if args.cmd == "watch":
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        pub.watch(interval=args.interval, once=args.once, stop=stop, move_on_success=args.move_on_success)
        return 0

    if args.cmd == "takedown":
        live = pub.ledger.live_posts(args.card_id, args.platform)
        if not live:
            print("nothing live in the ledger for that card")
            return 0
        if not args.yes:
            for e in live:
                print(f"would delete {e['platform']}: {e.get('url') or e.get('post_id')}")
            print("re-run with --yes to delete")
            return 1
        results = pub.takedown(args.card_id, args.platform)
        if args.json:
            print(json.dumps(results, indent=2))
        else:
            for r in results:
                print(f"{r['platform']}: {r['status']} - {r['message']}")
        return 0 if all(r["status"] == "deleted" for r in results) else 3

    if args.cmd == "log":
        entries = pub.ledger.live_posts(args.card, args.platform) if args.live else [
            e for e in pub.ledger.entries()
            if (not args.card or e.get("card_id") == args.card)
            and (not args.platform or e.get("platform") == args.platform)
        ]
        entries = entries[-args.limit:]
        if args.json:
            print(json.dumps(entries, indent=2, ensure_ascii=False))
        else:
            for e in entries:
                print(f"{e.get('ts')} {e.get('event'):<16} {e.get('card_id')} {e.get('platform'):<9} "
                      f"{e.get('url') or e.get('post_id') or e.get('error', '')}")
        return 0

    if args.cmd == "fingerprint":
        found = pub.cabinet.find(args.card_id)
        if not found:
            print("card not found", file=sys.stderr)
            return 1
        print(fingerprint(found[1]))
        return 0

    if args.cmd == "new-card":
        platforms = tuple(p for p in ALL_PLATFORMS if p in args.platforms) if args.platforms else ALL_PLATFORMS
        card_dir = approval.new_card(pub.cabinet, args.slug, args.media, platforms=platforms, brief=args.brief,
                                     ai_generated=not args.no_ai_generated)
        print(card_dir.name)
        print(f"created {card_dir} (v1). Fill in the captions in instructions.json, then run:\n"
              f"  grokbot-publisher preview {card_dir.name}", file=sys.stderr)
        return 0

    if args.cmd == "approve":
        return _approve(pub, args.card_id, args.version)

    if args.cmd == "revise":
        card_dir, version = approval.revise(pub.cabinet, args.card_id, args.reason)
        print(f"{card_dir.name} is now v{version} in /inbox/, not approved. Edit, preview, then approve v{version}.")
        return 0

    if args.cmd == "init":
        Cabinet(settings.cabinet).ensure()
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        print(f"cabinet ready at {settings.cabinet}")
        return 0

    if args.cmd == "doctor":
        from grokbot_publisher import doctor

        checks = doctor.run(pub, online=args.online)
        icons = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
        for level, name, msg in checks:
            print(f"[{icons[level]}] {name}: {msg}")
        return 1 if any(level == "fail" for level, _, _ in checks) else 0

    if args.cmd == "auth":
        oauth = OAuth(settings, pub.tokens, pub.http)
        if args.auth_cmd == "url":
            print(oauth.authorize_url(args.platform))
            print("\nOpen it, approve, then run:\n"
                  f"  grokbot-publisher auth finish {args.platform} '<the full URL you landed on>'",
                  file=sys.stderr)
        elif args.auth_cmd == "finish":
            info = oauth.finish(args.platform, args.redirect)
            print(f"connected {info['platform']}: {info['account'] or 'ok'}")
        elif args.auth_cmd == "set":
            pub.tokens.update(args.platform, access_token=args.access_token, refresh_token=args.refresh_token,
                              expires_in=args.expires_in, user_id=args.user_id, username=args.username)
            print(f"stored {args.platform} token in {settings.tokens_file}")
        elif args.auth_cmd == "refresh":
            from grokbot_publisher.platforms import REGISTRY
            from grokbot_publisher.platforms.base import Context

            ctx = Context(settings=settings, http=pub.http, tokens=pub.tokens, media=pub.media,
                          storage_factory=pub.storage_factory, card_id="auth")
            auth = REGISTRY[args.platform](ctx).auth()
            if not auth.refresh():
                print(f"{args.platform}: this token can't be refreshed; run `auth url {args.platform}`")
                return 1
            print(f"{args.platform}: refreshed at {iso()}")
        else:
            print(json.dumps(pub.tokens.status(), indent=2))
        return 0

    return 1


def _approve(pub: Publisher, card_id: str, version: int) -> int:
    """The human YES. It must be typed by a person: agents (Cursor, Claude, the
    Orchestrator) run commands without a terminal, so they are refused here."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("error: approve must be run by a person in an interactive terminal; agents can't approve posts",
              file=sys.stderr)
        return 1
    found = pub.cabinet.find(card_id) if valid_card_id(card_id) else None
    shown = fingerprint(found[1]) if found else None
    report = pub.publish(card_id, expect_version=version, dry_run=True)
    if report.refused_reasons:
        _print_report(report, as_json=False)
        return 2
    print(f"\n=== {card_id} v{version}: exactly what will be posted ===")
    for r in report.results:
        cost = f", est. cost ${r.est_cost_usd:.3f}" if r.est_cost_usd else ""
        print(f"\n[{r.platform}] privacy: {r.privacy or '-'}{cost}")
        if r.error:
            print(f"  PROBLEM: {r.error}")
        for name, value in (r.preview.get("fields") or {}).items():
            if value and name != "commentary":
                print(f"  {name}: {value}")
        for m in r.preview.get("media") or []:
            print(f"  media: {m['file']} {m['dimensions']}" + (f" {m['duration_s']}s" if "duration_s" in m else ""))
        for note in r.notes:
            print(f"  note: {note}")
    if any(r.status not in ("dry_run",) for r in report.results):
        print("\nSome posts have problems (above). Fix them before approving.")
        return 2
    answer = input(f"\nType the card id to approve v{version} for posting: ").strip()
    if answer != card_id:
        print("not approved")
        return 1
    card_dir = approval.approve(pub.cabinet, card_id, version, shown_sha256=shown)
    print(f"approved v{version} ({card_dir}). Publish with:\n"
          f"  grokbot-publisher publish {card_id} --expect-version {version}")
    return 0
