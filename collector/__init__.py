"""The Marketplace collector: reads Facebook Marketplace from a computer at home.

Facebook shows Marketplace only to a signed-in browser on a home connection,
which GitHub's runners are not. So this runs on a Mac that stays on: every
twenty to thirty minutes it opens each search in a browser signed in to
Facebook, keeps what the page itself received, seals it with the vault key
and sends it to the repository, where the bot judges and announces it like
any other car. It writes nothing of the bot's own; the bot stays the only
writer.

Run it with collector/run; see collector/README.md.
"""

VERSION = "1"
