# The Marketplace collector

Facebook shows Marketplace only to a browser that is signed in and on a home
connection. GitHub's runners are neither, so a computer at home does the
reading and the bot does everything else. This folder is that computer's half.

Every 20 to 30 minutes the collector opens each of your searches in its own
browser, signed in to Facebook, and keeps what the page itself received: the
results it arrived with and the ones that loaded as it scrolled. For a car
that passes your rules it also opens the listing once, for the exact
odometer and details, and sends what it found there with the car from then
on. It seals what it read with your vault key and sends it
to your repository, which starts **Check AutoTrader**. The bot applies your
rules, sends the alerts and updates the dashboard, exactly as for a car from
AutoTrader. The collector writes none of the bot's data.

What it searches is read from your watch: each active search becomes a
Marketplace search for the same make and model, years and prices, newest
first, within the search's distance (Marketplace offers up to 500 km). Change
a search on the dashboard and the next pass searches the new thing.

## Before you start

Facebook's terms do not allow automated collection. The collector reads
slowly, as a person browsing would, from your own home connection, and never
posts, messages or changes anything, but Facebook can still challenge or
restrict an account it suspects. Use an account you can afford to have asked
to confirm who it is. If Facebook signs the collector out or asks for
confirmation, the bot tells you, and nothing else is affected.

You need a Mac that stays on and signed in to its user account, with
[Homebrew](https://brew.sh) Python 3.10 or newer (`brew install python`) and
ideally Google Chrome. On power, set System Settings → Battery (or Energy) →
*Prevent automatic sleeping when the display is off*. While the collector
runs it also holds the Mac awake itself, but not with the lid closed: a
MacBook still sleeps when its lid is closed, unless it is on power with an
external display attached.

## Set up

In Terminal:

```sh
git clone https://github.com/<you>/<repo> ~/autotrader-watch
cd ~/autotrader-watch
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r collector/requirements.txt
.venv/bin/python -m playwright install chromium   # only if Google Chrome is not installed
collector/run setup
```

`setup` asks for two things and stores them in the macOS Keychain:

1. **A GitHub token.** On github.com: Settings → Developer settings →
   Personal access tokens → Fine-grained tokens → Generate new token.
   Repository access: only your watch's repository. Permissions:
   **Contents: Read and write**, nothing else. Pick an expiry date and put it
   in your calendar: when it expires the collector stops and the bot says so.
   This token can change the code your checks run with the passphrase, so
   guard it as you guard the passphrase: it lives in this Mac's Keychain and
   nowhere else.
2. **Your passphrase**: the one in the `WATCH_PASSPHRASE` secret, which also
   unlocks the dashboard. The collector uses it to read your searches and to
   seal what it sends.

It then checks both and lists the searches it will read. Next:

```sh
collector/run login     # a window opens: sign in to Facebook, then close it
collector/run once      # one pass now; says what it read
collector/run install   # run it from now on, and at every login
```

In the login window, also open Marketplace and set its location to where
you search from: Facebook remembers it for the account.

Within a few minutes the dashboard's **Status** tab has a **Facebook
Marketplace** section, and Marketplace cars appear with a *Marketplace* tag.
The first pass for each search records what is already for sale without
alerting; from then on a new car is an alert.

## A second Mac

Set the second one up the same way, with `collector/run setup --standby`. It
checks in but does not read while the first is being heard from. If the
first goes quiet for 75 minutes, or says Facebook has signed it out or asked
the account to confirm who it is, the second starts reading, and the bot
tells you it has taken over. When the first reads again, the second stands
by again. It reads only while the bot is taking in its own check-ins, so
the two never read at once because the bot has stopped taking batches in
(GitHub is down, or **Check AutoTrader** is switched off). It signs in to
Facebook separately (`collector/run login` on it too).

## Everyday

| Command | What it does |
|---|---|
| `collector/run status` | What is set up, whether it is running, what the last pass did |
| `collector/run once` | One pass now (`--now` reads even overnight, on standby, or while it holds off after Facebook signed it out) |
| `collector/run login` | Sign in again, after Facebook signs the collector out |
| `collector/run check` | Try the token and passphrase, and list the searches |
| `collector/run explain` | Read every search now and say where each car went and why; sends nothing |
| `collector/run test-alert` | Send one test alert to your phone through ntfy |
| `collector/run setup --secrets` | Replace the token or passphrase |
| `collector/run uninstall` | Stop it, and stop it starting at login |
| `collector/run forget` | Remove the service, the Keychain items and the browser profile |

To update: `git pull`, then
`.venv/bin/pip install -r requirements.txt -r collector/requirements.txt` for
any new versions it pins, then `collector/run install` to restart it.

Its log is `~/Library/Application Support/AutoTrader Watch/collector.log`.
Everything it keeps (settings, the browser profile, a small memory of which
cars it has opened) is in that folder, never in the clone.

## Settings

`~/Library/Application Support/AutoTrader Watch/settings.json`, written by
`setup`. Restart with `collector/run install` after editing.

| Setting | Default | Meaning |
|---|---|---|
| `every_minutes`, `jitter_minutes` | 25, 5 | A pass every 20 to 30 minutes |
| `quiet_start`, `quiet_end` | `00:30`, `06:30` | Overnight it only checks in, in this Mac's time |
| `details_per_cycle` | 3 | Listing pages opened per pass, for cars that pass your rules |
| `scrolls` | 2 | Screens of results per search beyond the first |
| `role` | `primary` | `standby` reads only when the primary has gone quiet or Facebook will not let it in |
| `takeover_after_minutes` | 75 | How long a standby waits for the primary |
| `show_browser` | `false` | Show the browser while it reads |
| `channel` | `chrome` | Use Google Chrome; empty for Playwright's Chromium |

These live on the Mac because only the Mac should decide how often it
visits Facebook. Each batch reports them, and the dashboard's **Status** tab
shows them, read-only, with when the next batch is due.

On the watch's side, from the dashboard's **Searches** tab:

| Setting | Default | Meaning |
|---|---|---|
| Marketplace, per search | on | Whether the search is also read on Marketplace. Switched off, its Marketplace cars are written off quietly and any batch still reading it is ignored; switched back on, its first read is a starting point, not a list of new cars. |
| Also counts as | none | Other spellings of the search's model. Taking one away drops the Marketplace cars only it let in. |
| `marketplace.radius_km` | each search's own | How far Marketplace is asked to look, at most 500 km |
| `marketplace.place` | from `near` | The word for your area in Marketplace's own addresses |
| `marketplace.exact` | loose | Exact matching reads fewer cars of other models, and may miss one titled another way. The same pages are read either way. |
| `marketplace.new_within_days` | 7 | A car Facebook dates further back is recorded, not announced, when it first appears: results are capped, so older cars drift into view as newer ones sell |
| `marketplace.gone_after_days` | 10 | A car unseen this long, while its search reads fine, is gone. A car Facebook marks sold is gone at once. |

## Why most cars read are not on your list

Marketplace has no model filter. A search for one model returns most cars of
that make nearby, so "read 60, on your list 3" is normal. The dashboard's
**Status** tab shows, per search, how many were read, how many were another
model (and which models), how many a rule hid, and how many are on your
list. To see every car and the reason for each:

```sh
collector/run explain
```

It reads each search once, the way a pass does, opens no listing page and
sends nothing. Each car is one line: its title, the model Facebook's own
fields give (when they give one), the model the bot read, price, kilometres
(`~` when Marketplace rounded them, as in "45K km"), place, seller, photo and
age, followed by the rule that hid it, if one did. It sorts cars with the
bot's own code, from the search results and from what a real pass already
found on a car's own page, as the bot does. A real pass also opens the page
of a car on your list once, and what it finds there (the exact kilometres,
a rebuilt title) can still hide a car not opened yet.

It waits while a pass is using the browser, and a pass waits for it, since
only one browser can use the collector's profile at a time.

If a car of yours is set aside as another model, look at how its title and
Facebook's fields spell the model: that is what to report, or to add as an
alternative spelling.

`collector/run explain --capture` also keeps what each page received, in
`~/Library/Application Support/AutoTrader Watch/captures/`, and prints an
outline of it: where the listings sit in Facebook's data, which fields they
carry, and a few example values, with names, ids and addresses masked. Share
the outline when a field comes out empty or wrong; the raw files stay on the
Mac. Delete the folder whenever you like.

## When something goes wrong

The bot alerts you when the collector has not checked in for two hours, when
Facebook signs it out, and when it checks in but has read nothing for nine
hours. On the Mac, `collector/run status` says which part is missing, and the
log says why.

- **Signed out, or asked to confirm**: `collector/run login`, sign in, close
  the window. Until you do, the collector only checks in, for 12 hours at a
  time, rather than knock on the same page every pass.
- **It cannot send**: after three sends GitHub refused in a row, the
  collector only checks in, reading nothing it could not deliver, until
  GitHub takes one again.
- **The token was refused**: it expired or lost its permission. Make a new
  one and run `collector/run setup --secrets`.
- **The passphrase does not open the vault**: it was changed; run
  `collector/run setup --secrets` with the new one.
- **Searches read nothing**: Facebook may have changed its pages. Run
  `collector/run explain --capture` and share the outline it prints; the
  reading itself is in `autotrader/marketplace.py`.
- **No alerts arrive**: `collector/run test-alert` sends one through the
  watch's ntfy topic, the channel a Mac can reach without the repository's
  secrets, and says whether ntfy took it. If you protected the topic with
  the `NTFY_TOKEN` secret, run it as `NTFY_TOKEN=... collector/run test-alert`.
  Other channels need secrets only the repository has; test those with
  **Check AutoTrader**.
