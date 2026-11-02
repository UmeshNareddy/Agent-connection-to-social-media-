# Stock Watch: Reinvest Wire watcher

Watches the [Reinvest Wire](https://app.meetkevin.com/data/wire) around the clock and
writes every new headline to files that your other agents can read.

**How it works:** the Wire page already checks for news about once a minute using your
login. The watcher connects to a Chrome window where you're logged in and *listens* to
those updates. It never calls Reinvest's servers itself and never sees your password or
login token.

> **Personal use only.** The headlines are Reuters content licensed to Reinvest
> subscribers. Let your own agents read them, but don't republish or share them.

## One-time setup (Mac)

1. Install the watcher (from this repo's folder):
   ```bash
   python3 -m pip install -e . 'playwright>=1.40'
   ```
2. Start a **separate** Chrome window for the watcher (this keeps your normal Chrome
   untouched; Chrome only allows this kind of connection on a separate profile):
   ```bash
   open -na "Google Chrome" --args --remote-debugging-address=127.0.0.1 \
     --remote-debugging-port=9222 --user-data-dir="$HOME/.reinvest-chrome-profile" \
     https://app.meetkevin.com/data/wire
   ```
3. **Log in to Reinvest in that new window** (first time only; it remembers you).
4. Copy the watchlist and edit it with your tickers:
   ```bash
   cp stock_watch/watchlist.example.yaml stock_watch/watchlist.yaml
   ```

> Security note: any program on your Mac can control that Chrome window through port
> 9222. Use it only for Reinvest, not for banking or email.

## Run it

```bash
python3 -m stock_watch.reinvest --watchlist stock_watch/watchlist.yaml
```

Leave it running. Keep the Mac awake (System Settings → Battery/Energy → prevent
sleeping) and leave the watcher's Chrome window open. You can minimize it.

On the first run it quietly records the headlines already on the page and only reports
headlines that arrive **after** it starts. To include the current page once, add
`--backfill`.

## What your other agent reads

Files go to `~/stock-watch/reinvest/` (change with `--out-dir`):

| File | Contents |
|---|---|
| `headlines.jsonl` | Every new headline, one JSON object per line, oldest first |
| `alerts.jsonl` | Only urgent ones: BREAKING, or matching your watchlist |
| `status.json` | Health: when the last update arrived, last error (e.g. logged out) |

Example line:

```json
{"storyId": "urn:newsml:reuters.com:20261102:nP8N45T03F:3", "headline": "S.KOREA PRESIDENT LEE: ...",
 "category": "Fed & Rates", "publishedUtc": "2026-11-02T01:08:25.989Z", "source": "Reuters",
 "isBreaking": true, "pinned": false, "urgent": true, "reasons": ["breaking", "category:Fed & Rates"],
 "seenAtUtc": "2026-11-02T01:09:02Z"}
```

Tell your other agent something like: *"Check `~/stock-watch/reinvest/alerts.jsonl` for
new lines and analyze them. If `status.json` shows an error, tell me."*

Updates to a story (Reuters "UPDATE 2-…" items) arrive with a new `storyId` version and
show up as new headlines.

## If something goes wrong

| Symptom | Fix |
|---|---|
| `Cannot reach Chrome at http://127.0.0.1:9222` | Start Chrome with the command in step 2 |
| `status.json` says logged out | Log in again in the watcher's Chrome window |
| `no feed update for 5 min; reloading` | Usually harmless: the watcher reloads the page itself |
