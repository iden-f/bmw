"""macwatch: the Mac-hosted watcher trial.

One long-running service on the owner's Mac reads AutoTrader.ca over HTTP and
Facebook Marketplace through the collector's signed-in browser, keeps every
listing it has ever seen in a SQLite archive with its complete price history
and saved photos, and sends alerts from a durable outbox to its own ntfy
topic. It runs beside the GitHub-hosted watcher, not instead of it: during
the trial GitHub remains the authoritative alert sender, and this service's
alerts are labelled so the two can be compared.

Nothing here writes the bot's vault, state or dashboard. It imports the
tested parser, filters and renderers from ``autotrader`` and the browser from
``collector``, and keeps its own data under its own folder.
"""

VERSION = "0.1.0"
