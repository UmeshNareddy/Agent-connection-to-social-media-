# -*- coding: utf-8 -*-
"""Tests for the Reinvest Wire watcher's offline logic."""

import json

from stock_watch.reinvest import (
    SeenStore,
    Watchlist,
    WireWatcher,
    extract_new,
    is_feed_response,
)


def _item(story_id, headline="Something happened", category="Earnings",
          breaking=False, published="2026-11-02T03:02:23Z"):
    return {"storyId": story_id, "headline": headline, "category": category,
            "imageUrl": None, "isBreaking": breaking, "publishedUtc": published,
            "source": "Reuters"}


def _payload(headlines, pinned=()):
    return {"status": "success", "isSuccess": True, "statusCode": 200, "error": None,
            "data": {"headlines": list(headlines), "pinnedItems": list(pinned),
                     "lastUpdatedUtc": "2026-11-02T03:04:05Z", "hasMore": False}}


def _lines(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_keyword_matching_tickers_are_case_sensitive():
    watch = Watchlist(keywords=["ON", "Nvidia"])
    assert watch.reasons(_item("a", "Stocks rally ON earnings")) == ["keyword:ON"]
    assert watch.reasons(_item("b", "Markets slip on rate fears")) == []
    assert watch.reasons(_item("c", "NVIDIA beats estimates")) == ["keyword:Nvidia"]
    assert watch.reasons(_item("d", "Nvidias of the world")) == []


def test_breaking_and_category_reasons():
    watch = Watchlist(categories=["Fed & Rates"])
    item = _item("a", category="fed & rates", breaking=True)
    assert watch.reasons(item) == ["breaking", "category:fed & rates"]
    assert Watchlist(breaking_is_urgent=False).reasons(item) == []


def test_extract_new_dedupes_and_sorts_oldest_first(tmp_path):
    seen = SeenStore(tmp_path / "seen.json")
    payload = _payload([_item("new", published="2026-11-02T03:00:00Z"),
                        _item("old", published="2026-11-02T01:00:00Z")],
                       pinned=[_item("pin", published="2026-11-01T00:00:00Z")])
    first = extract_new(payload, seen, Watchlist())
    assert [r["storyId"] for r in first] == ["pin", "old", "new"]
    assert first[0]["pinned"] is True and first[1]["pinned"] is False
    assert extract_new(payload, seen, Watchlist()) == []


def test_story_update_with_new_version_is_new(tmp_path):
    seen = SeenStore(tmp_path / "seen.json")
    extract_new(_payload([_item("urn:x:1")]), seen, Watchlist())
    assert [r["storyId"] for r in
            extract_new(_payload([_item("urn:x:2")]), seen, Watchlist())] == ["urn:x:2"]


def test_seen_store_is_bounded_and_persists(tmp_path):
    path = tmp_path / "seen.json"
    store = SeenStore(path, limit=3)
    for i in range(5):
        store.add(str(i))
    store.save()
    reloaded = SeenStore(path, limit=3)
    assert "0" not in reloaded and "1" not in reloaded
    assert all(str(i) in reloaded for i in (2, 3, 4))


def test_first_run_is_a_silent_baseline(tmp_path):
    watcher = WireWatcher(tmp_path, Watchlist())
    assert watcher.handle_payload(_payload([_item("a"), _item("b", breaking=True)])) == []
    assert _lines(tmp_path / "headlines.jsonl") == []

    records = watcher.handle_payload(_payload([_item("c", breaking=True), _item("a")]))
    assert [r["storyId"] for r in records] == ["c"]
    assert [r["storyId"] for r in _lines(tmp_path / "headlines.jsonl")] == ["c"]
    alerts = _lines(tmp_path / "alerts.jsonl")
    assert [(a["storyId"], a["reasons"]) for a in alerts] == [("c", ["breaking"])]


def test_restart_skips_baseline_and_backfill_outputs_everything(tmp_path):
    WireWatcher(tmp_path, Watchlist()).handle_payload(_payload([_item("a")]))
    restarted = WireWatcher(tmp_path, Watchlist())
    assert [r["storyId"] for r in
            restarted.handle_payload(_payload([_item("a"), _item("b")]))] == ["b"]

    fresh = tmp_path / "fresh"
    backfilled = WireWatcher(fresh, Watchlist(), backfill=True)
    assert len(backfilled.handle_payload(_payload([_item("a"), _item("b")]))) == 2


def test_error_payload_is_recorded_in_status(tmp_path):
    watcher = WireWatcher(tmp_path, Watchlist())
    assert watcher.handle_payload({"isSuccess": False, "error": "nope"}) == []
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert "nope" in status["lastError"]["message"]


def test_watchlist_load(tmp_path):
    path = tmp_path / "w.yaml"
    path.write_text("keywords: [NVDA]\ncategories: [Earnings]\nbreaking_is_urgent: false\n",
                    encoding="utf-8")
    watch = Watchlist.load(path)
    assert watch.keywords == ["NVDA"] and watch.categories == ["Earnings"]
    assert watch.breaking_is_urgent is False
    assert Watchlist.load(None).keywords == []


def test_is_feed_response():
    url = ("https://app-api.meet-kevin.com/api/news/feed/day?dayStartUtc=2026-11-01T04%3A00"
           "%3A00.000Z&offset=0&pageSize=200")
    assert is_feed_response(url, "GET")
    assert not is_feed_response(url, "OPTIONS")
    assert not is_feed_response("https://app-api.meet-kevin.com/api/news/Categories/", "GET")
