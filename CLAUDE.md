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
- The dashboard is published at most every `PAGE_MIN_GAP_MINUTES` (default 120)
  and at least every `PAGE_MAX_AGE_MINUTES` (default 360), set as repository
  variables. Each publish is a billed Pages build.
- The check interval is the main cost: one check is about one billed minute.
  The owner wants a check every 30 minutes by day and every 2 hours from 02:00
  to 08:00 Pacific time (`health.overnight`, in `config.py`; the interval
  itself is `health.expected_interval_minutes` in the vault's config.json).
  What fires most checks is the Marketplace collector on the owner's Mac
  (`collector/`, `repository_dispatch` type `marketplace`), not an outside
  timer: each pass starts a run, so its overnight pause (`quiet_start`,
  `quiet_end`, `quiet_every_minutes` in its settings.json) is what saves
  minutes. A run the bot turns away still bills a minute.

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
