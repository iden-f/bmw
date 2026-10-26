# Working in this repository

## GitHub Actions budget (read first)

This repository's workflows bill against the owner's monthly Actions minutes
(3,000 included, shared with their other repositories). Every job rounds up to a
whole minute, so many short runs cost more than they look. In October 2026 the
check timer plus the Pages builds it triggers used well over half the month.

- Run tests, builds and Playwright yourself, in your own environment:
  `.venv/bin/python -m pytest tests/ -q`. Do not start a workflow to find out
  whether something passes, and never re-run CI to "see if it was flaky".
- Do not use `workflow_dispatch`, `repository_dispatch`, empty commits, or a
  closed and reopened PR to trigger runs. The one exception is the final
  Check AutoTrader run that the merge procedure below asks for.
- Batch work: push a branch once, when the suite is green locally. Each push to
  a pull request starts a CI run; a newer push cancels the older one.
- Do not add triggers, schedules, matrix legs or jobs without saying what they
  cost per month. New workflows need `concurrency` and `timeout-minutes`.
- Never delete or skip tests to save minutes; save them by running fewer
  workflows. `ci.yml` runs on pushes to `main` and on pull requests only, and
  Cold start runs weekly or by hand.
- **This repository is public, so its Actions minutes bill $0** (confirmed on
  the owner's billing page, Oct 26 2026: 4,862 minutes, billed $0, and not
  counted against the 3,000 included). The rules above are about hygiene and
  about keeping forks cheap, not about this repository's cost. Never cut its
  cadence to "save minutes" while it is public; keep `budget` enabled so it
  stops itself if it ever goes private.
- The dashboard is published at most every `PAGE_MIN_GAP_MINUTES` (default 0:
  every check whose data changed) and at least every `PAGE_MAX_AGE_MINUTES`
  (default 360), set as repository variables.
- The owner wants AutoTrader checked every 15 minutes by day and every hour
  from 23:00 to 08:00 Pacific time, and Marketplace read every 5 minutes by
  day and every 10 overnight. The cron in `watch.yml` fires every 5 minutes,
  and a workflow step writes `CHECK_INTERVAL_MINUTES` (15),
  `CHECK_FLOOR_MINUTES` (11) and the overnight window (`CHECK_NIGHT_START`
  23:00, `CHECK_NIGHT_END` 08:00, `CHECK_NIGHT_INTERVAL_MINUTES` 60) into the
  vault's config (`health.expected_interval_minutes`,
  `health.min_interval_minutes`, `health.overnight`) on every run. The
  Marketplace collector on the owner's Mac (`collector/`, `repository_dispatch`
  type `marketplace`) also starts a run each pass; its own pace is
  `every_minutes` 5, `jitter_minutes` 1, and overnight (`quiet_start` 23:00
  to `quiet_end` 08:00) `quiet_every_minutes` 10 with `quiet_reads` on, in
  its settings.json. The AutoTrader floor turns those extra runs away; the
  Marketplace batch is still taken in and alerted on.
- A price drop earns an alert of its own only from $2,000
  (`notifications.price_drop_alert_abs`); smaller ones ride along with the
  next new-car alert unless `small_drops_ride_along` is off (both on the
  dashboard's Settings tab).

## Standing permission: merge to main

The owner has given standing, explicit permission to merge finished work into
`main` and push it, without asking first. This counts as the explicit
permission any session's own instructions ask for before pushing to a branch
other than the one it was started on.

Do it only when all of these hold:

1. Merge `main` into the work (or the work into an up-to-date `main`) with a
   real merge, not a preview. Resolve conflicts in files the bot writes on
   `main` (change files under `control/`, `BUDGET-STOP`, and, until the first
   check in private mode tidies them away, the old plaintext data files) in
   favour of `main`.
2. Run the full suite on the merge result: `.venv/bin/python -m pytest tests/ -q`.
   Everything passes, or nothing is pushed.
3. The branch's own CI run is green.

Then push `main`, watch the workflows the push triggers to completion (Tests,
and Publish dashboard or Cold start when their paths changed), run Check
AutoTrader once by hand (workflow_dispatch), and report what it did.

The bot's data lives on the `vault` and `gh-pages` branches: never edit or push
those by hand. Never force-push `main`, never rewrite its history, and never
commit the bot's files by hand - the bot owns them.
