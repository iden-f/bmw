# Working in this repository

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
