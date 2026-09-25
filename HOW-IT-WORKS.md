# How it works

The bot is one Python program that runs for about a minute on a GitHub
Actions runner. It reads a few search pages, works out what changed, tells
you, and saves its records. Everything else here exists to make that
reliable, private, and easy to read. Setup and everyday use are in the
[README](README.md).

## A check, start to finish

`watch.yml` does all of this in one job, so a check costs one runner minute:

1. **Guard.** If `BUDGET-STOP` exists on `main`, stop before installing
   anything.
2. **Open the vault.** `vault pull` fetches the `vault` branch into `vault/`;
   `vault open` decrypts it into the working tree (`config.json`,
   `state.json`, `EVENTS.md`, `docs/events.json`, `docs/thumbs/`,
   `diagnostics/`, `archives/`), or creates a new vault on a first run. All of
   these are in `.gitignore`.
3. **Set up.** `setup --non-interactive` makes sure something can reach you (a
   random ntfy topic when no channel is configured) and that alerts link to
   `https://<owner>.github.io/<repo>`.
4. **Apply changes.** `control control` applies any change files the
   dashboard sent.
5. **Check.** `run` reads every search: fetch, parse, filter, compare with
   state, alert, copy photos, audit, and write `docs/data.json`. A scheduled
   firing within 90 minutes of the last check stands down here without
   contacting AutoTrader.
6. **Record.** `events --notify` updates the ledger of market firsts.
   Steps 3 to 6 send their output only to `run.log`, which is sealed with the
   rest.
7. **Save.** `vault seal` re-encrypts whatever changed; `vault push` replaces
   the `vault` branch with a single commit.
8. **Tidy `main`.** The `control/` files this check read are removed, applied
   or refused; every private path (`vault paths` lists them) is kept out of
   git; and a `BUDGET-STOP` the budget guard wrote is committed.
9. **Publish.** If the dashboard's data changed, `vault site site` builds the
   site and `vault publish site` replaces `gh-pages` with a single commit of
   it.

A search that fails still saves what the others found; the job is marked
failed at the end instead.

## Modules

All in `autotrader/`, in the order the data moves.

**Reading the site**

- **`urls`**: parses a pasted autotrader.ca link into something to fetch and
  describe in words.
- **`http`**: one paced, retrying client. A per-check request budget (250 by
  default) is a hard stop covering result pages, detail pages and photos.
- **`parser`**: four independent strategies, confined to the page's declared
  results; see [Reading only the results](#reading-only-the-results).
- **`validate`**: decides whether a parse can be trusted. A page that loads and
  parses to nothing is a failure, not an empty market, and a first check that
  looks wrong records nothing.
- **`shape`**: fingerprints each results page, so a site redesign shows up as
  a warning before it shows up as silence.
- **`enrich`**: reads a new car's own page for the exact price, odometer,
  colour and photos. Capped per check.
- **`geo`**: how far a car is from you, from its city and province.
- **`diagnose`**: describes a page that parsed to nothing, under
  `diagnostics/`.

**Deciding**

- **`filters`**: your rules on top of the link. A car a rule hides is kept and
  explained; a result the `models` rule rejects is discarded.
- **`state`**: the ledger. Every car, its price history, what you were told,
  and the last 60 run summaries, written atomically in a `finally` block.
  Change detection lives here: new, price drop, price rise, now priced, gone,
  back, and back inside your rules.
- **`invariants`**: audits the ledger after every check. Every car must be
  told about, owed an alert, or quiet for a recorded reason. A violation fails
  the check and names the entries in `diagnostics/invariants.json`.
- **`insight`**: every number the dashboard shows, worked out once, including
  the weekly digest.
- **`events`**: the ledger of the first time each kind of market event
  happened (`EVENTS.md`), and the watchdog's silence alarm.
- **`budget`**: a ledger of runner minutes, and the `BUDGET-STOP` guard.

**Telling you**

- **`render`**: one message per check, headed with the car it is most about.
- **`notifiers`**: one isolated sender per channel. A failed alert is held and
  retried on the next check; a channel rejected twice switches itself off and
  says why.
- **`provision`**: first-run setup: a random ntfy topic and the dashboard
  address.
- **`qr`**: a QR encoder with no dependencies, for the ntfy topic.

**Keeping and publishing**

- **`vault`**: the encryption, the `vault` and `gh-pages` branches, and the
  published site.
- **`control`**: reads a change from the dashboard and applies it whole or not
  at all.
- **`dashboard`**: builds `docs/data.json`, and refuses to write anything
  shaped like a credential.
- **`thumbs`**: small copies of listing photos, at most 24 new photos per
  check, pruned when a car goes.
- **`archive`**: a per-car archive (metadata only by default), with
  retention.

**Plumbing**: **`cli`** (every command), **`runner`** (one check),
**`config`** (settings in `config.json`, secrets only from the environment),
**`listing`** (the car record), **`lock`** (one check at a time), **`clock`**
(one clock, settable in tests), **`words`** (plurals and durations, defined
once), **`ui`** (a local server that lets the page save settings).

## Reading only the results

Each results page is read four ways, `jsonld`, `embedded_json`, `anchors` and
`regex`, and the strategy with the most rows wins while the others fill in
fields it missed. The Status tab shows every strategy's score, so a weakening
strategy is visible before cars stop arriving.

"Most rows" is the wrong measure on its own, because a results page also
carries recommendation rails, and those cars sit in the same front-end data as
the real results with real ids and prices. So the parser asks the page which
cars are its results, two ways, and keeps the union:

- the schema.org `ItemList`: explicit, but it leaves out call-for-price cars,
  because a schema.org `Offer` needs a price;
- the front-end state's own result list (`props.pageProps.listings`, as
  opposed to `props.pageProps.recommendations`): it includes those, but it is
  a private structure that can change without notice.

Three safeguards fall back to reading the whole page, which is noisier but
cannot lose a car: a page that declares nothing is read whole; declared ids no
strategy can build are ignored; and when the page says it has N results and
fewer resolved, the declared set is a sample, not an answer.

On a read confined this way the `models` rule stands down: the site's own
answer beats matching a free-text field. When a parse carries no model at all,
the rule stands down too, and the check says so, rather than discarding a
whole search over a parser fault.

## The vault

`autotrader/vault.py`, in a format a browser can open with WebCrypto alone.

- **Keys.** PBKDF2-HMAC-SHA256 (passphrase, 16-byte salt, 600,000 iterations)
  gives a master key. HMAC-SHA256 derives two subkeys from it: one encrypts,
  one names files.
- **Files.** `ATW` · version byte · flags byte (bit 0: gzip, bit 1: padded) ·
  12-byte nonce · AES-256-GCM ciphertext and tag. The file's logical name is
  the associated data, so one encrypted file cannot be passed off as another.
  Before encryption the content is length-prefixed and padded with zeros to a
  multiple of 4 KiB, or 64 KiB for photos and `data.enc`, so sizes reveal
  little: a photo's exact size would otherwise match the public original.
- **`meta.json`** is public by design: the salt, the iteration count, and a
  check value that confirms a passphrase without decrypting anything else. The
  site carries the same as `lock.json`.
- **Directories** (`docs/thumbs/`, `diagnostics/`, `archives/`) are sealed
  file by file under keyed hashes, with an encrypted index to map them back.
- **Unchanged files stay unchanged.** AES-GCM output differs every time, so
  sealing compares plaintexts first; otherwise every check would rewrite
  every file.
- **A new passphrase.** When `WATCH_PASSPHRASE_PREVIOUS` opens the vault and
  `WATCH_PASSPHRASE` does not, unlocking re-encrypts everything under the new
  one with a new salt, and `vault open` then moves ntfy to a new topic: whoever
  held the old passphrase could read the old one. [The README](README.md#privacy) has the steps.

| Branch | Holds | Written by |
|---|---|---|
| `main` | Code, workflows, documents; change files under `control/` until a check reads them; `BUDGET-STOP` if written | You, and the check's tidy step |
| `vault` | `meta.json` and ciphertext: `config.enc`, `state.enc`, `events.enc` and the rest | Every check; the watchdog, with a lease |
| `gh-pages` | The page (`index.html`, `app.js`, `sw.js`, manifest, icons), `lock.json`, `data.enc`, and photos as `thumbs/<hash>.bin` | A check whose data changed; Publish dashboard |

Both data branches are a single commit, replaced on every write, so neither
keeps a history. `vault push --lease` refuses to replace a vault that
changed since it was pulled; the watchdog pushes that way so it can never
overwrite a check that saved in the meantime.

## Workflows

| File | Name | Runs | What it does |
|---|---|---|---|
| `watch.yml` | Check AutoTrader | Fires every 30 minutes (`7,37 * * * *`); on a push to `control/`; on `repository_dispatch` of type `check`; by hand, optionally as a dry run | The check, as above. A push only counts when the repository owner made it. |
| `events.yml` | Watchdog | Four times a day (`41 1,7,13,19 * * *`), and Mondays (`20 14 * * 1`) | Alerts when no check has succeeded for six hours, and sends the weekly digest. |
| `pages.yml` | Publish dashboard | A push to `docs/` or the dashboard code; by hand | Rebuilds and publishes the site from the vault. |
| `coldstart.yml` | Cold start | Weekly (`23 6 * * 0`); a push to `autotrader/` | Sets up from nothing, watches a generic public search, and checks it once. Never pushes, never touches the vault. |
| `ci.yml` | Tests | Every push except change files, and pull requests | The suite on three Python versions, and a command-line smoke test. |

Public repositories on standard runners pay nothing for Actions, and the bot
labels each minute it spends as exempt or not. Should the repository ever draw
on an allowance (made private, or moved to a larger runner), it projects the
month, and past 85% of `budget.included_minutes` (a 3,000-minute allowance
unless you set your plan's) it commits `BUDGET-STOP`, alerts you, and stops
checking until you delete the file. Every firing is billed, including one
that stands down, so the schedule in `watch.yml` sets the cost.

## Changes from the dashboard

A static page cannot write to a repository, and a phone should not carry a
token. So a change is a file:

1. The page wraps the change as `{"id", "at", "changes"}` - a one-time id and
   the time - pads it to a whole kilobyte, encrypts it with the vault key, and
   opens GitHub's "new file" page on `main` with
   `control/<time>-<random>.enc` filled in. Neither the file name, its size
   nor the commit message says what the change is.
2. Committing it triggers **Check AutoTrader**, which acts on a push only when
   the repository owner made it.
3. `python -m autotrader control control` decrypts each file and applies it
   whole or refuses it whole. A file sealed with any other key, an unsealed
   file, an id already applied, or a change older than 14 days is refused.
   Each outcome, with its reason, is kept in state and shown under **Your
   recent changes** on the Searches tab.
4. The check removes the files it read from `main`.

Valid actions: `set-rule`, `add-search`, `remove-search`, `mute-listing`,
`unmute-listing`, `shortlist`, `unshortlist`, `dismiss`, `undismiss`,
`set-channel`, `note`.

## The dashboard

`docs/` is the whole site: `index.html`, `app.js`, a service worker, a
manifest and icons. No build step, no framework.

- **Locked.** The page fetches `lock.json` and shows only the lock screen
  until the passphrase checks out against it. Then it decrypts `data.enc` and
  the photos in the browser. Without a `lock.json`, as when served by
  `python -m autotrader ui`, it reads `data.json` directly.
- **Offline.** The service worker keeps the page, the data and the photos.
  Offline, the page shows the saved copy and says how old it is.
- **Updates.** `sw.js` carries a build stamp written on every publish, so a
  changed page installs a new worker and an installed app offers a reload.
- **Words.** One word per idea, in the page and in the alerts alike. A
  **check** is one run. A **listing** is one car. **Hidden** means a rule of
  yours excludes it, and it is still kept, counted and explained.
  **Discarded** means it was never one of the search's results and is not
  kept. **Gone** means it left the site; the bot never calls a car sold,
  because it cannot know. **Back** means gone and then listed again.

## Messages it can send

Besides the alerts about cars, these are all of them.

| Subject | What happened | What to do |
|---|---|---|
| **AutoTrader watcher is working** | The first check read the site and the parse looks right. | Nothing. It says this once. |
| **AutoTrader watcher: the first check looks wrong** | A first check read something that does not look like a results page, so it recorded nothing. | [A search fails to parse](#a-search-fails-to-parse). |
| **AutoTrader watcher needs attention** | A search failed three checks in a row, or has been unreadable for six hours. It names the search and the error. | [A search fails to parse](#a-search-fails-to-parse). |
| **AutoTrader changed how its pages are built** | The results page changed shape. The bot is still reading it, by another route. | Nothing yet. Worth a look if it becomes "needs attention". |
| **AutoTrader watcher has gone quiet** | No check has succeeded for six hours and nothing has started since. It says whether this looks like GitHub dropping runs or something changing. | [Checks have stopped](#checks-have-stopped). |
| **AutoTrader watcher is running and failing** | Checks start and fail every time. | [Checks have stopped](#checks-have-stopped). |
| **AutoTrader watcher covered only N% of yesterday** | The schedule delivered under half the checks asked of it. At most once a day. | An outside timer; see the README's Schedule section. |
| **AutoTrader watcher: its own records do not add up** | A bookkeeping rule failed after a check. Sent when the set of broken rules changes. | Read `diagnostics/invariants.json` in an opened vault. |
| **This month's runner minutes are heading over** | The repository is drawing on an allowance, and the projection passes the ceiling. Nothing has stopped yet. | Make the repository public again, or thin the `cron` in `watch.yml`. |
| **The watcher has stopped: this month's minutes are spent** | It committed `BUDGET-STOP` and will not check until the file is gone. | Delete `BUDGET-STOP` to resume. |
| **Switched off <channel> notifications** | A channel rejected the bot's credentials twice. | Fix the secret, then switch the channel back on locally with `set notifications.channels.<name>.enabled true`. |
| **AutoTrader watcher: your alerts moved** | The ntfy topic changed. | Subscribe again from the QR code on the **Status** tab. |
| **AutoTrader: your last N days** | The weekly digest. | Read it, or not. |

## Testing

`python -m pytest -q`. The suite makes no network requests: every page it
reads is one AutoTrader really served, kept in `tests/fixtures/`, and
`tests/test_offline.py` holds it to that.

| File | Covers |
|---|---|
| `test_vault.py`, `test_vault_cycle.py` | The cipher, unlocking, a new passphrase, and a whole check's vault handling against a real git remote. |
| `test_private_site.py` | The locked site in a real browser: a stranger sees a lock, the owner unlocks, and a change sealed by the page opens in Python. |
| `test_parser.py`, `test_parser_hardening.py`, `test_only_the_results.py` | Every strategy, hostile and broken pages, and confinement to the declared results. |
| `test_invariants.py`, `test_chaos.py` | The bookkeeping rules, and deliberate damage: corrupt state, a truncated page, revoked credentials. |
| `test_workflows.py` | The workflows parse, stay one job per check, and keep their schedule promises. |
| `test_docs.py` | These documents against the code: commands, files, links, and the numbers they quote. |

`sh scripts/time-gate.sh` runs the whole suite at month ends, a leap day and a
year boundary, in six timezones.

## When something goes wrong

Start with the dashboard's **Status** tab. If the latest cells of **When it
checked** are filled, the bot is reading the site and anything else can wait.

To see what a check wrote, open the vault locally (see the README) with
`vault open --log` and read `run.log`.

### Checks have stopped

1. **Actions → Check AutoTrader.** Recent red runs mean it is starting and
   failing: the failed step's error says why, and `run.log` says more.
2. **No runs at all?** A fork runs nothing until Actions is enabled on its
   Actions tab, and GitHub disables scheduled workflows in a repository with
   no activity for 60 days; the workflow's page then shows an *Enable
   workflow* button. Check that `BUDGET-STOP` is not on `main`, and that the
   `WATCH_PASSPHRASE` secret exists: without it every check stops at *Open the
   vault* and says so.
3. **Runs, but far apart?** That is GitHub's scheduler, and the most common
   cause of all. An outside timer (`scripts/keep-time.sh`, in the README's
   Schedule section) takes it out of the path.

*Run workflow* on **Check AutoTrader** always asks for a check straight away.

### A search fails to parse

1. **Is it the site or the search?** Open the search link in a browser. Cars
   there and none in the bot is a parser problem; nothing there either means
   the link needs changing on the Searches tab.
2. **Ask the parser.** `python -m autotrader doctor --live` fetches the page
   and prints each strategy's count and a sample of what it read. Any
   strategy above zero means the bot is still reading.
3. **Capture the page.** `python -m autotrader capture --raw` saves what the
   site serves now under `diagnostics/`. Make it a fixture in
   `tests/fixtures/` and write the failing test first. A fixture is public, so
   capture a generic search rather than your own.
4. **Add a strategy rather than editing one.** `STRATEGIES` in
   `autotrader/parser.py` is an ordered tuple of independent
   `(name, function)` pairs; a new one cannot break the others.

Two warnings read like a parser fault and are not always one. "Read N
listings and none passed your rules" is a narrow search on a working parser.
"All N results the site returned were a different model" means the link
widened, or the parser stopped reading the model: check the sample in
`doctor --live` before widening `models`.

While any of this is true, nothing is called gone on a search the bot could
not read, so a parser fault costs alerts, not records.

### The vault will not open

The *Open the vault* step, or `vault open` locally, says which of these it is:

- **"Add a repository secret named WATCH_PASSPHRASE"**, **"WATCH_PASSPHRASE
  is not set"** or **"must be at least 12 characters"**: add or fix the
  repository secret.
- **"does not open this vault"**: the secret is not the passphrase the vault
  was made with. Set it back, or finish changing it as
  [the README](README.md#privacy) describes.
- **"vault format N is newer than this bot"**: bring your fork's code up to
  date (*Sync fork* on GitHub).
- **"could not be decrypted"** on a single file: the `vault` branch was edited
  by hand or damaged.

If the passphrase is lost (set a new `WATCH_PASSPHRASE` first), or the vault
is beyond saving, delete the `vault` branch and run a check. It starts over with an empty vault and a new ntfy
topic; add your searches again and subscribe from the Status tab.

The dashboard has nothing to show until the first check has created
`gh-pages` and Pages is set to serve it. After a passphrase change it asks for
the new one once **Publish dashboard** or the next check has published.
