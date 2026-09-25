#!/usr/bin/env sh
# Stands in for curl in tests/test_keep_time.py: prints a canned body and
# status so every branch of scripts/keep-time.sh can be exercised.
#
# The default body is assigned rather than written as "${FAKE_BODY:-{}}",
# because POSIX sh parses that as the expansion ${FAKE_BODY:-{ followed by a
# literal }. Every canned reply came out with one brace too many, and the
# first place that showed was a hand-run of the script printing
# `GitHub said: {"message":"..."}}` - which reads as a bug in the thing being
# handed over.
body=${FAKE_BODY-'{}'}
printf '%s\n%s' "$body" "${FAKE_CODE:-204}"
