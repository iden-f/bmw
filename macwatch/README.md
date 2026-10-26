# The Mac-hosted watcher (trial)

One service on the always-on Mac that reads AutoTrader.ca and Facebook
Marketplace itself, keeps everything it has ever seen in a SQLite archive
with full price history and saved photos, and sends alerts from a durable
outbox to its **own ntfy topic**. It runs beside the GitHub-hosted watcher,
not instead of it.

During the trial:

- **GitHub stays the authoritative alert sender.** Nothing here touches the
  vault, `state.json`, the dashboard or the workflows. The existing
  collector keeps running and keeps sending its batches.
- **The Mac alerts to a separate topic**, `<your topic>-mac-trial`, with
  every title prefixed `[Mac] `. Subscribe to both in the ntfy app and
  compare: which arrived first, which was missed, which was noise.
- **The searches and rules are the watch's own.** They are read from the
  vault (with the collector's Keychain items) and cached, so a search
  changed on the dashboard reaches the Mac within half an hour, and GitHub
  being down changes nothing: the Mac keeps collecting with the cached copy.
- A per-search `notify: "archive"` in the watch config makes an
  **archive-only** collection here: stored, charted, photographed, never
  pushed. A top-level `pinned` list of listing URLs is tracked silently.

## What it keeps

Under `~/Library/Application Support/AutoTrader Watch/macwatch/`:

| Where | What |
|---|---|
| `data/macwatch.sqlite3` | listings, every price observation, every revision, sightings per read, collection membership, notification outbox, your marks and notes, settings for the scheduler |
| `media/` | photos, content-addressed by SHA-256, verified as images before they count, up to 12 per listing, kept when the ad goes |
| `backups/` | `macwatch/run backup` copies via SQLite's online backup API |
| `logs/macwatch.log` | the service's log |
| `settings.json` | the service's own settings (cadence, topic, token) |
| `config-cache.json` | the last good copy of the watch's config |

Gone is a status, not a deletion. A photo budget pauses capture and says
so; it never prunes.

## Set up (on the Mac, once)

```sh
cd ~/autotrader-watch && git pull
.venv/bin/pip install -r macwatch/requirements.txt
macwatch/run setup        # reads the vault, picks the topic, imports the bot's records
macwatch/run once         # one pass now: AutoTrader over HTTP, Marketplace via the browser
macwatch/run install      # run it from now on, at every login, restarted if it stops
macwatch/run tailscale    # prints the one command that shares the app on your tailnet
```

`setup` imports the GitHub watcher's current records (every car, its price
history as the bot kept it, your shortlist) so history starts from what you
already have, and the first read of each search is a **baseline**: it records
what is for sale and announces nothing. From then on a new car is an alert.

The first read on Marketplace shares the collector's browser profile and
its lock, so the two take turns; neither opens the profile while the other
has it.

## The app

`tailscale serve --bg --https=443 http://127.0.0.1:8765` puts the app at
`https://idenair.<tailnet>.ts.net/` for devices on your Tailscale network
only (Serve, not Funnel: nothing public). Open
`/login?token=<token>` once per device; `macwatch/run status` prints the
token. Add it to the Home Screen for a full-screen app.

- **Cars**: what is for sale, photo-led, filtered to your rules by default;
  switch to everything seen, gone cars, one search, sort by price or km.
- A car: saved photos, full asking-price history with every observed
  change, facts, which searches it is in and why, every notification
  decision about it (sent, suppressed and why), notes, shortlist, mute.
- **Archive**: each search with how much it has saved since when, and a
  JSON export with full history.
- **Status**: both sources, when they last read and when they are next
  due, the vault copy's age, the outbox, recent reads. **Check now** and
  **Test alert**.
- **Settings**: cadence per source, quiet hours, photos per listing, the
  trial topic. Saved to the Mac; no Terminal, no GitHub.

## Everyday

| Command | What it does |
|---|---|
| `macwatch/run status` | what is set up, last passes, counts, the token |
| `macwatch/run once [--site autotrader\|marketplace]` | one pass now |
| `macwatch/run test-alert` | one message to the trial topic |
| `macwatch/run backup --verify` | a consistent copy into `backups/`, opened and counted |
| `macwatch/run import-legacy` | import the bot's records again (adds nothing twice) |
| `macwatch/run token --rotate` | a new app token |
| `macwatch/run uninstall` | stop the service; data stays |

Its log: `~/Library/Application Support/AutoTrader Watch/macwatch/logs/macwatch.log`.

## How a pass works

1. The scheduler (one thread, 15-second ticks) starts a source when its
   persisted due time has passed and it is not already running. A slow pass
   delays the next one rather than stacking; a restart resumes rather than
   replaying.
2. **AutoTrader**: every active search through the bot's own fetcher,
   parser and budget; a new car's own page for the exact price, odometer and
   photos. **Marketplace**: every planned query through the collector's
   browser; a new car within the rules gets its own page, up to
   `details_per_cycle` per pass.
3. **Ingest**, one transaction per search: store or update the car, append
   a revision when facts change, append a price event when the asking price
   moves (a results card is compared with the last card and a detail page
   with the last detail page, never across), keep membership and a
   sighting, decide what changed (new, price drop, back on sale, now within
   your rules, gone) and write every decision to the outbox: `pending` to
   send, or `suppressed` with the reason (archive-only, baseline, below the
   drop threshold, muted, the kind is switched off, hidden by a rule).
4. The **outbox worker** posts pending rows to the trial topic with
   backoff; a refused request is not retried; a restart between send and
   acknowledgement can duplicate, never lose, and the row says which.
5. The **media worker** downloads queued photos from allowed hosts only,
   checks the bytes are an image, stores them by hash.

Absence from a partial read is not evidence: a car is `not_seen_recently`
after one complete, confined read without it and `unavailable` after two.
Marketplace reads are never complete, so there a car goes after ten quiet
days.

## Comparing the two systems

Both topics on the phone for a few weeks. Things to note: time from a car
appearing to each alert, cars one found and the other did not, duplicate or
noisy alerts, and days the Mac was asleep or offline (Status shows the last
successful read per source; the service pings `heartbeat_url` after each
good pass if you give it one). The outbox table is the Mac's complete
record of what it decided and why; the GitHub watcher's Status tab is the
other side.

## Boundaries

Same as the watch's: it reads slowly, from your own machine, never posts,
messages or changes anything, and stops and says so when Facebook signs it
out or asks the account to confirm who it is. Facebook's and AutoTrader's
terms restrict automated access; running locally does not change that.
