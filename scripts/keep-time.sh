#!/usr/bin/env sh
# Ask the AutoTrader watcher for a check, from anywhere, over one HTTP request.
#
# Run it from an outside timer to keep checks on time when GitHub's scheduler
# drops runs. It sends a repository_dispatch event and explains whatever
# GitHub answers; a reply it does not recognise is printed as it came.
#
# Usage
#   GITHUB_TOKEN=github_pat_... sh scripts/keep-time.sh
#   sh scripts/keep-time.sh --token github_pat_...   (same thing)
#   sh scripts/keep-time.sh --from my-mac            (name this timer)
#   sh scripts/keep-time.sh --repo owner/name        (another repository)
#   sh scripts/keep-time.sh --cron                   (quiet; for crontab)
#
# The token is a fine-grained personal access token for this repository only,
# with Repository permissions > Contents set to "Read and write". GitHub files
# repository_dispatch under Contents, not Actions.
#
# A crontab line that asks every hour (extra requests are deduplicated):
#   17 * * * * GITHUB_TOKEN=github_pat_... sh /path/to/keep-time.sh --cron
#
# Exit codes
#   0  the check was asked for
#   1  something is wrong, and the message says what

set -eu

REPO=""               # --repo owner/name; defaults to this clone's origin
EVENT="check"          # must match `repository_dispatch: types:` in watch.yml
FROM="timer"
QUIET=0
TOKEN="${GITHUB_TOKEN:-${GH_TOKEN:-}}"

while [ $# -gt 0 ]; do
  case "$1" in
    --token) TOKEN="$2"; shift 2 ;;
    --from)  FROM="$2";  shift 2 ;;
    --repo)  REPO="$2";  shift 2 ;;
    --cron)  QUIET=1;    shift ;;
    -h|--help) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 1 ;;
  esac
done

say() { [ "$QUIET" -eq 1 ] || printf '%s\n' "$*"; }

if [ -z "$REPO" ]; then
  origin=$(git -C "$(dirname "$0")" remote get-url origin 2>/dev/null || true)
  REPO=$(printf '%s' "$origin" | sed -n 's#.*github\.com[:/]\([^/]*/[^/]*\)$#\1#p' | sed 's/\.git$//')
fi
[ -n "$REPO" ] || { echo "Which repository? Pass --repo owner/name." >&2; exit 1; }
die() { printf '%s\n' "$*" >&2; exit 1; }

[ -n "$TOKEN" ] || die "No token. Set GITHUB_TOKEN, or pass --token github_pat_...
Make one at github.com -> Settings -> Developer settings -> Personal access
tokens -> Fine-grained tokens. Give it access to ONLY $REPO, and under
Repository permissions set Contents to 'Read and write'. Nothing else."

# The name is printed on the dashboard, so it is bounded here rather than
# trusted: lowercase, digits, dot and dash, 24 characters.
FROM=$(printf '%s' "$FROM" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9.-' | cut -c1-24)
[ -n "$FROM" ] || FROM="a-timer"

BODY=$(printf '{"event_type":"%s","client_payload":{"from":"%s"}}' "$EVENT" "$FROM")

say "Asking $REPO for a check (as \"$FROM\")..."

# Overridable so tests can exercise every branch below with a stub that
# prints a canned reply.
CURL="${KEEP_TIME_CURL:-curl}"

OUT=$($CURL -sS -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  -H "Content-Type: application/json" \
  "https://api.github.com/repos/$REPO/dispatches" \
  -d "$BODY" -w '\n%{http_code}' 2>&1) || die "curl could not reach GitHub:
$OUT
Check this machine has a working internet connection."

CODE=$(printf '%s' "$OUT" | tail -n1)
REPLY=$(printf '%s' "$OUT" | sed '$d')

case "$CODE" in
  204)
    say "OK. GitHub accepted it (HTTP 204, which is the success code - there is"
    say "no reply body, and that is correct)."
    say ""
    say "Look at https://github.com/$REPO/actions within about ten seconds."
    say "A run called \"Check AutoTrader\" should be there, marked"
    say "repository_dispatch. If the bot checked recently it will finish in"
    say "about fifteen seconds without reading the site - that is the"
    say "deduplication working, not a failure."
    exit 0 ;;
  401)
    die "HTTP 401 - GitHub does not recognise the token.
It is mistyped, or it has expired. Fine-grained tokens expire; make a new one." ;;
  403)
    die "HTTP 403 - the token is real but not allowed to do this.
Almost always: the token is missing 'Contents: Read and write'. Edit the token
at github.com -> Settings -> Developer settings -> Personal access tokens, open
it, and check Repository permissions. (repository_dispatch is filed under
Contents, not Actions - that is the usual surprise.)
GitHub said: $REPLY" ;;
  404)
    die "HTTP 404 - the token cannot SEE $REPO.
GitHub returns 404 rather than 403 for a repository a token has no access to,
so this is an access problem and not a typo in the name (though check the name
too). Edit the token and make sure $REPO is in 'Only select repositories'." ;;
  415|422)
    die "HTTP $CODE - GitHub rejected the request body.
This is a bug in this script rather than in anything you did. Please report it
with this line: $REPLY" ;;
  000)
    die "curl got no HTTP response at all.
Usually a proxy, a firewall, or no DNS. Try: curl -sS https://api.github.com" ;;
  *)
    die "HTTP $CODE - not a code this script knows about.
GitHub said: $REPLY
Nothing was broken by trying. If this persists, that reply is the thing to
search for." ;;
esac
