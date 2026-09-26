# AutoTrader Watch

Watches AutoTrader.ca search links and tells your phone when a car appears or
gets cheaper. It runs on GitHub Actions in your own copy of this repository,
keeps everything personal encrypted under one passphrase, and publishes a
locked dashboard on GitHub Pages. No server, no other accounts, no cost.

> **Fork it; do not use someone else's.** Each copy belongs to whoever runs it.
> A running copy's dashboard shows a stranger a lock screen and nothing else,
> its alerts go to a topic only its owner knows, and only the repository owner
> can change what it watches. To watch your own searches, fork this repository
> and follow [Set up your own](#set-up-your-own).

## What it does

- **New listings.** One alert per check, however many cars turned up, sent at
  high priority and headed with the car:
  *New 2019 Example Coupe · $41,000 · Toronto · 52,000 km*.
- **Price drops.** Every car's price is tracked, and a drop is measured from
  the price you last heard. Any drop of at least 1% and $250 is an alert. Set
  a bar ([Rules](#rules)) and only drops that big alert on their own; smaller
  ones wait and ride along in the next new-car alert. Cuts add up, so a car
  cut $1,000 at a time still reaches a $3,000 bar.
- **Other news about a car.** A car your rules were hiding coming into range,
  a call-for-price car naming a figure, and a car back on the market for less.
  Price rises, cars leaving the site and every relisting are on the dashboard,
  and can be alerts too.
- **It says when it breaks.** A search that fails three checks in a row, or
  stays unreadable for six hours, raises an alert. A separate watchdog raises
  one when no check has succeeded for six hours, because a watch cannot
  report its own absence.
- **A weekly digest** of what the market did, every Monday.
- **A dashboard** with every car, its photo and price history, the market as
  a whole, and the bot's own health. It installs to a phone's home screen and
  works offline, labelled with the age of what it shows.
- **Changes from your phone.** Add or remove a search, change a rule, or
  shortlist, mute, dismiss or annotate a car, all from the dashboard. No
  token, no terminal.

## Set up your own

All you need is a GitHub account.

1. **Fork** this repository. Leave *Copy the main branch only* ticked, and
   keep the fork public: Actions and Pages cost nothing on a public
   repository, and everything personal is encrypted. A private fork spends
   Actions minutes on each of the 48 firings a day and needs a paid plan for
   Pages. If your fork did copy a `vault` or `gh-pages` branch, delete both
   so you start with a vault of your own.
2. **Choose a passphrase.** Settings → Secrets and variables → Actions → New
   repository secret, named `WATCH_PASSPHRASE`, 12 characters or more. Use
   four or five random words: anyone can download the encrypted data and try
   guesses offline, and nobody can recover the passphrase for you. Keep it in
   a password manager.
3. **Enable Actions.** Actions tab → *I understand my workflows, go ahead and
   enable them*.
4. **Run the first check.** Actions → **Check AutoTrader** → *Run workflow*.
   With no searches yet, it creates your vault, sets up a private ntfy topic
   for alerts, and publishes the dashboard to a new `gh-pages` branch.
5. **Turn on Pages.** Settings → Pages → *Deploy from a branch* → `gh-pages`,
   `/ (root)` → Save.
6. **Open your dashboard** at `https://<you>.github.io/<repo>/` once Pages has
   built it (a minute or two), and unlock it with the passphrase.
7. **Add a search.** Run the search you want on autotrader.ca, copy the
   address bar, paste it under **Add a search** on the dashboard's
   **Searches** tab, and press **Add this search**. GitHub opens its "new
   file" page with an encrypted change already filled in; signed in as the
   repository owner, press **Commit changes**. A check starts within a minute
   or so and applies it, and the Searches tab shows the outcome under **Your
   recent changes**.
8. **Get the alerts on your phone.** On the **Status** tab: install the free
   ntfy app, point the phone's camera at the QR code, and subscribe.

That is all. From then on a check is due every two hours, and runs by itself.

## Privacy

| Anyone can see | Only you can read |
|---|---|
| The code, the workflows and these documents | Your searches, rules, marks and notes |
| The dashboard's page and its encrypted data | Every car, its price history and photos |
| When checks run, and roughly how much data there is (files are padded to fixed sizes) | Your ntfy topic, the run log, the market ledger |
| When a change was sent from the dashboard | What the change says |

- **Encrypted at rest.** AES-256-GCM, with the key derived from
  `WATCH_PASSPHRASE` by PBKDF2-SHA256 at 600,000 iterations. The bot's data
  lives on a `vault` branch and the dashboard's copy on `gh-pages`, both as
  ciphertext only. Photos are stored under keyed hashes and padded to one
  size, so neither a file's name nor its size points at a listing.
- **Quiet logs.** Actions logs are public, so the bot writes its output to a
  log that is sealed into the vault. The public log shows which steps ran and
  how they ended.
- **A strong passphrase.** Anyone can test guesses against the public
  `lock.json` offline, so use four or more random words. The bot refuses a
  new passphrase that repeats too few characters.
- **The passphrase stays in your browser.** The dashboard turns it into a key
  with the browser's own WebCrypto and decrypts in the page. With **Keep this
  device unlocked** ticked (the default), the derived key, never the
  passphrase, stays in that browser until you press **Lock** in the header.
  Untick it on a device you share: then nothing about your cars outlives the
  tab. **Lock** forgets everything this dashboard stored on the device and
  locks its other open tabs.
- **One origin per account.** Every GitHub Pages site of an account is served
  from the same `<you>.github.io` origin and can read what the others store.
  Do not publish untrusted pages under the same account, or give the
  dashboard a custom domain.
- **Changing the passphrase.** Add the current one as a
  `WATCH_PASSPHRASE_PREVIOUS` secret, set `WATCH_PASSPHRASE` to the new one,
  and run **Check AutoTrader** once: it re-encrypts the vault under the new
  passphrase and moves ntfy to a new topic, so subscribe again from the Status
  tab. Then delete `WATCH_PASSPHRASE_PREVIOUS`. Each device asks for the new
  one on its next visit. Copies made under the old passphrase - old change
  files in the repository's history, or a snapshot someone kept - stay
  readable with it; if it truly leaked, start again in a fresh repository.
- **A lost passphrase cannot be recovered.** Set a new `WATCH_PASSPHRASE`,
  delete the `vault` branch and run a check: it starts over with an empty
  vault and a new ntfy topic, and you add your searches again.
- **Only you can drive it.** Changes are applied only if they are sealed with
  your key. Each carries a one-time id and its time, so an old change cannot
  be replayed, and each is padded so its size does not say what it does.
- **What encryption does not cover.** An alert can be read by the service
  that carries it, and on ntfy by anyone who knows the topic. GitHub's
  runners hold the data in the clear while a check runs. Anything you commit
  yourself, such as a test fixture, is public.

## Alerts

With no other channel configured, the first check sets up
[ntfy](https://ntfy.sh): free, and no account. Alerts travel through the
public ntfy.sh server to a topic named with 20 random characters, stored only
in the vault and shown only on your unlocked dashboard. Anyone who learned
the topic could read your alerts, so keep it to yourself.

Other channels are repository secrets too (Settings → Secrets and variables →
Actions), and work alongside ntfy. Each switches itself on once its secrets
are present, except SMS, which costs money and also needs
`set notifications.channels.twilio.enabled true` (see
[Running it locally](#running-it-locally)).

| Channel | Secrets | Where they come from |
|---|---|---|
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Message @BotFather, send `/newbot`, copy the token. Message your new bot once, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id`. |
| Discord | `DISCORD_WEBHOOK_URL` | Server Settings → Integrations → Webhooks → New Webhook → Copy URL. |
| Slack | `SLACK_WEBHOOK_URL` | Create a Slack app → Incoming Webhooks → Add New Webhook. |
| Email | `GMAIL_USER`, `GMAIL_APP_PASSWORD` | Turn on 2-Step Verification, then create an App Password. Not your normal password. Alerts go to the same address. |
| SMS | `TWILIO_SID`, `TWILIO_TOKEN`, `TWILIO_FROM`, `TWILIO_TO` | A Twilio account. Costs money per message; the others are free. |
| ntfy, protected | `NTFY_TOKEN` | An access token, for your own ntfy server or a reserved topic. |

A channel whose credentials are rejected twice switches itself off, tells you
through one that still works, and says why on the Status tab. An alert that
could not be delivered is held and sent on the next check.

## The dashboard

- **Feed**: what changed, newest first, and which of it reached your phone.
- **Listings**: every car, with its photos, price history and your marks.
- **Market**: what the cars say together: median asking prices, how long cars
  stay listed, how often prices move, and a deal score with its own track
  record.
- **Searches**: what is being watched and how each search is reading. Add a
  search, remove one or change its rules here.
- **Status**: when it checked, how well the schedule is keeping time, the
  alert channels, and the QR code for your phone.

A car one of your rules hides is kept, counted and explained rather than
dropped: click the count to see it and the rule.

## Rules

A search link carries most of what you want. For the rest, each search has
rules of its own. The Searches tab changes `max_price`, `min_year`,
`max_year` and `max_distance_km`; `set` changes any of them (see
[Running it locally](#running-it-locally)). Anything a search does not set
falls through to the global value.

| Rule | What it does |
|---|---|
| `min_price`, `max_price`, `min_year`, `max_year`, `max_mileage_km` | Bounds. |
| `near`, `max_distance_km` | Where you are and how far you would drive. AutoTrader does not reliably apply the distance in a link, so the bot measures it from each listing's city. A car it cannot place is never hidden. |
| `provinces` | A region list, when a radius is the wrong shape. |
| `models` | The model the search is for, e.g. `["Civic"]`. A result that is a different model is discarded, not hidden. |
| `include_keywords`, `exclude_keywords`, `exclude_sellers` | Words and sellers. |
| `require_price` | Do not alert on call-for-price cars. They are still tracked. |

And for alerts, globally:

| Setting | What it does |
|---|---|
| `notifications.price_drop_alert_abs` | The bar: a drop at least this big is an alert of its own, and a smaller one rides along. `0`, the default, alerts every drop over the floor at once. |
| `notifications.price_drop_min_pct`, `notifications.price_drop_min_abs` | The floor: a drop must clear both (1% and $250) to be mentioned at all. |
| `notifications.small_drops_ride_along` | `false` keeps drops under the bar on the dashboard only. |
| `notifications.notify_on.*` | Which kinds of change alert: `new`, `price_drop`, `priced`, `qualified` and `errors` are on; `price_rise`, `removed` and `relisted` are off. `unpriced` follows `require_price`. |
| `notifications.quiet_hours` | Hold alerts overnight. They arrive after, not never. |
| `notifications.channels.ntfy.priority_new` | The ntfy priority of an alert with a new car in it, `high` by default. |

## Schedule

A check is due every two hours. GitHub runs scheduled workflows on a
best-effort basis and drops runs, often whole stretches of them, so **Check
AutoTrader** fires every 30 minutes and a firing that lands within 90 minutes
of the last check stands down without contacting AutoTrader. The Status tab
shows which two-hour windows had a check and what started each one.

For a clock that does not depend on GitHub's scheduler, point an outside timer
at it. `scripts/keep-time.sh` asks for a check through `repository_dispatch`,
using a fine-grained token with **Contents: Read and write** on your fork and
nothing else:

```sh
GITHUB_TOKEN=github_pat_... sh scripts/keep-time.sh --repo <you>/<repo> --from laptop
```

It prints what GitHub replied and what to do about it. Run it hourly from
cron with `--cron` (silent unless something is wrong), or have a hosted cron
service send the same request. Extra requests are deduplicated like the
schedule. The token expires on the date you chose, so put that date in a
calendar.

With [cron-job.org](https://cron-job.org) (free), create a job with:

| Field | Value |
|---|---|
| URL | `https://api.github.com/repos/<you>/<repo>/dispatches` |
| Schedule | fires every 30 minutes |
| Request method | `POST` |
| Headers | `Authorization: Bearer <token>`, `Accept: application/vnd.github+json` |
| Request body | `{"event_type":"check","client_payload":{"from":"cron-job"}}` |

A test run answers `204` when it works. The Status tab then shows checks kept
by "cron-job (an outside timer)".

## Stopping it

To pause, disable **Check AutoTrader** and **Watchdog** (Actions → the
workflow → ⋯ → *Disable workflow*), and enable them again to carry on. To
stop for good, also disable **Cold start**, set Settings → Pages → Branch to
*None*, and delete the `vault` and `gh-pages` branches, or delete the whole
repository. Alerts already delivered stay where they were delivered.

## Running it locally

From a clone of your fork:

```sh
pip install -r requirements.txt
read -rs WATCH_PASSPHRASE && export WATCH_PASSPHRASE   # typed, not shown or kept
python -m autotrader vault pull         # fetch the encrypted data
python -m autotrader vault open --log   # decrypt it here; git ignores it
```

Then:

```sh
python -m autotrader list                # what is being watched
python -m autotrader doctor              # config, links, channels, stored data
python -m autotrader doctor --live       # fetch the site and show what the parser read
python -m autotrader run --dry-run       # a whole check that changes and sends nothing
python -m autotrader ui                  # the dashboard, able to save settings
python -m autotrader test-notify         # a sample alert to every channel
python -m autotrader verify --limit 10   # re-read cars from the site and compare
python -m autotrader weekly              # the weekly digest, printed
python -m autotrader add "<search link>" # watch a search
python -m autotrader set max_price 40000 --search "<name>"
python -m autotrader remove <id> --forget
python -m autotrader forget --yes        # drop cars no search watches
python -m autotrader setup --new-topic   # move alerts to a fresh ntfy topic
```

Changes made here stay here until you save them back, and the next check
publishes them:

```sh
python -m autotrader vault seal
python -m autotrader vault push --lease  # refused if a check saved in the meantime
```

If the push is refused, pull and open again and redo the change. `--lease`
also stops a vault made on this machine from replacing the real one.

## Tests

```sh
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
```

The suite makes no network requests: it reads pages AutoTrader really served,
kept in `tests/fixtures/`. The browser tests skip themselves unless
Playwright's Chromium is installed under `/opt/pw-browsers`.
`PY=python sh scripts/time-gate.sh` runs the whole suite at awkward dates in
awkward timezones.

## More

[HOW-IT-WORKS.md](HOW-IT-WORKS.md) covers the design (modules, a check from
start to finish, the vault, the workflows, change requests, the parser) and
what to do when something goes wrong.

This reads a public website for personal use. Keep the pace polite: the
defaults pause between requests and cap each check at a fixed number of them.

## Licence

MIT.
