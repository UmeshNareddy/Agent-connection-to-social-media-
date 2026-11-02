# -*- coding: utf-8 -*-
"""Reinvest (app.meetkevin.com) Wire watcher.

The Wire page polls ``app-api.meet-kevin.com/api/news/feed/day`` about once a
minute with the user's own login. This watcher attaches to a dedicated,
already-logged-in Chrome over the DevTools protocol and *listens* to those
responses — it never calls the API itself, so it sends no extra traffic and
never touches the login token.

New headlines (deduplicated by ``storyId``) are appended to JSONL files that
another agent can tail:

  headlines.jsonl  every new headline
  alerts.jsonl     only urgent ones (BREAKING, or matching the watchlist)
  status.json      watcher health (last feed seen, last error)

Run:  python -m stock_watch.reinvest --watchlist stock_watch/watchlist.example.yaml
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import yaml
from loguru import logger

WIRE_URL = "https://app.meetkevin.com/data/wire"
WIRE_HOST = "app.meetkevin.com"
FEED_PATH = "/api/news/feed/day"
DEFAULT_CDP_URL = "http://127.0.0.1:9222"
DEFAULT_OUT_DIR = Path.home() / "stock-watch" / "reinvest"
MAX_SEEN = 5000

# All-caps words this short are treated as tickers and matched case-sensitively,
# so "ON" or "ALL" does not fire on the everyday words "on" / "all".
_TICKER_RE = re.compile(r"^\$?[A-Z]{1,5}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Watchlist:
    keywords: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    breaking_is_urgent: bool = True

    def __post_init__(self) -> None:
        self._patterns = [(k, _keyword_pattern(k)) for k in self.keywords if k.strip()]
        self._categories = {c.strip().lower() for c in self.categories if c.strip()}

    @classmethod
    def load(cls, path: Path | None) -> "Watchlist":
        if path is None:
            return cls()
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected a mapping with keywords/categories")
        return cls(
            keywords=[str(k) for k in data.get("keywords") or []],
            categories=[str(c) for c in data.get("categories") or []],
            breaking_is_urgent=bool(data.get("breaking_is_urgent", True)),
        )

    def reasons(self, item: dict) -> list[str]:
        """Why this headline is urgent; empty list means it is not."""
        reasons = []
        if self.breaking_is_urgent and item.get("isBreaking"):
            reasons.append("breaking")
        category = str(item.get("category") or "").strip().lower()
        if category and category in self._categories:
            reasons.append(f"category:{item.get('category')}")
        headline = str(item.get("headline") or "")
        for keyword, pattern in self._patterns:
            if pattern.search(headline):
                reasons.append(f"keyword:{keyword}")
        return reasons


def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    keyword = keyword.strip()
    flags = 0 if _TICKER_RE.match(keyword) else re.IGNORECASE
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(keyword) + r"(?![A-Za-z0-9])", flags)


class SeenStore:
    """storyIds already handed out, bounded and persisted across restarts."""

    def __init__(self, path: Path, limit: int = MAX_SEEN):
        self.path = path
        self.limit = limit
        self._ids: OrderedDict[str, None] = OrderedDict()
        if path.exists():
            try:
                for story_id in json.loads(path.read_text(encoding="utf-8")):
                    self._ids[str(story_id)] = None
            except (ValueError, TypeError):
                logger.warning(f"Ignoring unreadable seen-store {path}")

    def __contains__(self, story_id: str) -> bool:
        return story_id in self._ids

    def __len__(self) -> int:
        return len(self._ids)

    def add(self, story_id: str) -> None:
        self._ids[story_id] = None
        self._ids.move_to_end(story_id)
        while len(self._ids) > self.limit:
            self._ids.popitem(last=False)

    def save(self) -> None:
        _atomic_write_text(self.path, json.dumps(list(self._ids)))


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def iter_feed_items(payload: Any) -> Iterable[dict]:
    """Yield headline dicts from a feed response, pinned items included."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return
    for key, pinned in (("pinnedItems", True), ("headlines", False)):
        for item in data.get(key) or []:
            if isinstance(item, dict) and item.get("storyId"):
                yield {**item, "_pinned": pinned}


def extract_new(payload: Any, seen: SeenStore, watch: Watchlist) -> list[dict]:
    """Return unseen headlines as output records (oldest first) and mark them seen."""
    records = []
    for item in iter_feed_items(payload):
        story_id = str(item["storyId"])
        if story_id in seen:
            continue
        seen.add(story_id)
        reasons = watch.reasons(item)
        records.append({
            "storyId": story_id,
            "headline": item.get("headline"),
            "category": item.get("category"),
            "publishedUtc": item.get("publishedUtc"),
            "source": item.get("source"),
            "isBreaking": bool(item.get("isBreaking")),
            "pinned": item["_pinned"],
            "urgent": bool(reasons),
            "reasons": reasons,
            "seenAtUtc": utc_now(),
        })
    records.sort(key=lambda r: str(r.get("publishedUtc") or ""))
    return records


class WireWatcher:
    """Feed-response handler: dedupe, classify, write output files."""

    def __init__(self, out_dir: Path, watch: Watchlist, backfill: bool = False):
        self.out_dir = out_dir
        self.watch = watch
        self.seen = SeenStore(out_dir / "seen.json")
        # A fresh install would otherwise dump the ~200 headlines already on the
        # page as "new"; record them silently unless asked to backfill.
        self.baseline_pending = len(self.seen) == 0 and not backfill
        self.status: dict[str, Any] = {"startedUtc": utc_now(), "lastFeedUtc": None,
                                       "lastError": None, "headlinesWritten": 0,
                                       "alertsWritten": 0}
        out_dir.mkdir(parents=True, exist_ok=True)
        self.write_status()

    def handle_payload(self, payload: Any) -> list[dict]:
        if not isinstance(payload, dict) or payload.get("isSuccess") is False:
            self.record_error(f"feed returned an error: {str(payload)[:200]}")
            return []
        records = extract_new(payload, self.seen, self.watch)
        self.status["lastFeedUtc"] = utc_now()
        self.status["feedLastUpdatedUtc"] = (payload.get("data") or {}).get("lastUpdatedUtc")
        self.status["lastError"] = None
        if self.baseline_pending:
            self.baseline_pending = False
            logger.info(f"Baseline: {len(records)} existing headlines marked as seen")
            records = []
        self._append("headlines.jsonl", records)
        alerts = [r for r in records if r["urgent"]]
        self._append("alerts.jsonl", alerts)
        self.status["headlinesWritten"] += len(records)
        self.status["alertsWritten"] += len(alerts)
        self.seen.save()
        self.write_status()
        for r in records:
            tag = "🚨" if r["urgent"] else "·"
            logger.info(f"{tag} [{r['category']}] {r['headline']}")
        return records

    def record_error(self, message: str) -> None:
        logger.warning(message)
        self.status["lastError"] = {"atUtc": utc_now(), "message": message}
        self.write_status()

    def write_status(self) -> None:
        _atomic_write_text(self.out_dir / "status.json", json.dumps(self.status, indent=2))

    def _append(self, name: str, records: list[dict]) -> None:
        if not records:
            return
        with open(self.out_dir / name, "a", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def is_feed_response(url: str, method: str) -> bool:
    return method == "GET" and urlparse(url).path.rstrip("/").endswith(FEED_PATH)


def run(cdp_url: str, watcher: WireWatcher, stale_minutes: float) -> None:
    """Attach to Chrome and process feed responses until interrupted."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Playwright is required: python -m pip install 'playwright>=1.40'")

    pending: list[Any] = []

    def on_response(response: Any) -> None:
        if is_feed_response(response.url, response.request.method):
            pending.append(response)

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(cdp_url)
        except Exception as exc:  # noqa: BLE001 - surface any connect failure plainly
            sys.exit(f"Cannot reach Chrome at {cdp_url} ({exc}). Start it first; see "
                     "stock_watch/README.md.")
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        context.on("response", on_response)
        page = next((pg for pg in context.pages if WIRE_HOST in pg.url), None)
        if page is None:
            page = context.new_page()
            page.goto(WIRE_URL)
        elif "/data/wire" not in page.url:
            page.goto(WIRE_URL)
        logger.info(f"Watching {page.url} — output in {watcher.out_dir}")

        last_feed = time.monotonic()
        while True:
            page.wait_for_timeout(1000)
            while pending:
                response = pending.pop(0)
                if response.status in (401, 403):
                    watcher.record_error("Reinvest says you are logged out; log in again "
                                         "in the watcher's Chrome window")
                    continue
                if response.status != 200:
                    watcher.record_error(f"feed HTTP {response.status}")
                    continue
                try:
                    payload = response.json()
                except Exception as exc:  # noqa: BLE001 - body may be evicted or not JSON
                    watcher.record_error(f"could not read feed body: {exc}")
                    continue
                watcher.handle_payload(payload)
                last_feed = time.monotonic()
            if time.monotonic() - last_feed > stale_minutes * 60:
                watcher.record_error(f"no feed update for {stale_minutes:g} min; reloading")
                if page.is_closed():
                    page = context.new_page()
                page.goto(WIRE_URL)
                last_feed = time.monotonic()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Watch the Reinvest Wire for new headlines")
    parser.add_argument("--cdp-url", default=DEFAULT_CDP_URL,
                        help=f"Chrome DevTools address (default {DEFAULT_CDP_URL})")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                        help=f"where output files go (default {DEFAULT_OUT_DIR})")
    parser.add_argument("--watchlist", type=Path, help="YAML with keywords/categories")
    parser.add_argument("--stale-minutes", type=float, default=5,
                        help="reload the page if no feed arrives for this long (default 5)")
    parser.add_argument("--backfill", action="store_true",
                        help="on first run, output headlines already on the page too")
    args = parser.parse_args(argv)

    watcher = WireWatcher(args.out_dir.expanduser(), Watchlist.load(args.watchlist),
                          backfill=args.backfill)
    try:
        run(args.cdp_url, watcher, args.stale_minutes)
    except KeyboardInterrupt:
        logger.info("Stopped")


if __name__ == "__main__":
    main()
