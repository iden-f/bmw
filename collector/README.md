# The Marketplace collector

Facebook shows Marketplace only to a browser that is signed in and on a home
connection. GitHub's runners are neither, so a computer at home does the
reading and the bot does everything else. This folder is that computer's half.

Every 20 to 30 minutes the collector opens each of your searches in its own
browser, signed in to Facebook, and keeps what the page itself received: the
results it arrived with and the ones that loaded as it scrolled. For a car
that passes your rules it also opens the listing once, for the exact
odometer and details. It seals what it read with your vault key and sends it
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
runs it also holds the Mac awake itself.

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
first goes quiet for 75 minutes it starts reading, and the bot tells you it
has taken over. When the first comes back, the second stands by again. It
signs in to Facebook separately (`collector/run login` on it too).

## Everyday

| Command | What it does |
|---|---|
| `collector/run status` | What is set up, whether it is running, what the last pass did |
| `collector/run once` | One pass now (`--now` reads even overnight or on standby) |
| `collector/run login` | Sign in again, after Facebook signs the collector out |
| `collector/run check` | Try the token and passphrase, and list the searches |
| `collector/run setup --secrets` | Replace the token or passphrase |
| `collector/run uninstall` | Stop it, and stop it starting at login |
| `collector/run forget` | Remove the service, the Keychain items and the browser profile |

To update: `git pull`, then `collector/run install` to restart it.

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
| `role` | `primary` | `standby` reads only when the primary has gone quiet |
| `takeover_after_minutes` | 75 | How long a standby waits for the primary |
| `show_browser` | `false` | Show the browser while it reads |
| `channel` | `chrome` | Use Google Chrome; empty for Playwright's Chromium |

On the watch's side, `marketplace.radius_km` in the config overrides every
search's radius, and `marketplace.gone_after_days` (10) is how long a car can
go unseen before it counts as gone. A car Facebook marks sold is gone at once.
A car Facebook dates more than `marketplace.new_within_days` (7) back is
recorded but not announced when it first appears: results are capped, so
older cars drift into view as newer ones sell.

## When something goes wrong

The bot alerts you when the collector has not checked in for two hours, when
Facebook signs it out, and when it checks in but has read nothing for six
hours. On the Mac, `collector/run status` says which part is missing, and the
log says why.

- **Signed out, or asked to confirm**: `collector/run login`, sign in, close
  the window.
- **The token was refused**: it expired or lost its permission. Make a new
  one and run `collector/run setup --secrets`.
- **The passphrase does not open the vault**: it was changed; run
  `collector/run setup --secrets` with the new one.
- **Searches read nothing**: Facebook may have changed its pages. Run
  `collector/run once --now` and look at the log; the reading itself is in
  `autotrader/marketplace.py`.
