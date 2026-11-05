# Counted rate-limit dry run

Live posting stays paused until the rate-limit safety work is **verified**, not
felt. This page documents the one command that produces that verification as
counts, so the un-halt decision has an objective exit condition.

```bash
# from a source checkout (PYTHONPATH so the tree is the code under test):
PYTHONPATH="$PWD/src" python -m xpst.rate_limit_dry_run --json /tmp/xpst-dry-run.json

# from an installed venv:
~/.venvs/xpst/bin/python -m xpst.rate_limit_dry_run --json /tmp/xpst-dry-run.json
```

Exit code `0` means every invariant held **and** two runs produced identical
counts. Anything else is a failure and the summary names what broke.

## What it runs

A scripted matrix over the real pipeline — `UploadService.upload_to_platform` →
`retry_operation` → the real platform uploaders → `CooldownStore` — for every
destination and every fault shape the hardening work is supposed to survive:

| destination id | provider boundary the fake sits at |
|---|---|
| `x` | fake twikit client (`create_tweet`, `upload_media`) |
| `instagram` | fake `httpx.AsyncClient` for the Graph API publishing path |
| `instagram_session` | fake instagrapi client (the mode this host runs) |
| `facebook` | `httpx.MockTransport` behind the real Graph client |
| `youtube` | fake `googleapiclient` `videos().insert()` request |
| `tiktok` | fake `httpx.AsyncClient` for the Content Posting API route |

Fault shapes per destination: a healthy answer, `429` with Meta's numeric
throttle code and no hint, `429` with `Retry-After: 900`, a `403` rate-limit
body, and a `500`. On top of that matrix the run exercises the four states that
decide whether pausing is safe: a restart with the persisted cooldown, a 24-hour
hard pause, and cross-destination isolation (one cooling destination must not
block another).

## Why it cannot post for real

Every provider answer is fabricated, and the whole matrix runs under a socket
guard that refuses any outbound `connect()` (loopback is allowed for the event
loop's self-pipe). The summary reports `network.real_connections_attempted`,
which must be `0`; a non-zero value fails the run. Nothing needs credentials,
nothing reads the network, and no provider sees a byte.

## The reset, stated plainly

Cooldown state lives in `rate_cooldowns.json` under the config dir, and it
survives a restart by design. That is also how a second dry run can silently
measure the first run's leftovers. So:

* every scenario runs in its own **fresh isolated config dir**;
* the cooldown file is asserted absent before the post is attempted, and
  `reset.scenarios_with_leftover_cooldown_file` must be `0`;
* a reused `--work-dir` is wiped per run number before the matrix starts, so
  re-running the command in the same scratch root is still a clean measurement;
* the deliberate restart proofs are the only scenarios that *must* see a
  persisted file, and they are counted separately
  (`reset.restart_proofs_with_persisted_state`);
* nothing under `~/.xpst` is read or written.

## Reading the summary

The JSON carries `totals` (posts that would have gone out, rate-limit
responses, cooldowns engaged, deferred posts, dropped requests, unaccounted
posts), a per-scenario `scenarios` list, and `cooldown_decisions` — one entry per
decision with the `declared_retry_after_s` the provider asked for, the
`cooldown_pause_s` actually applied, and whether it was honoured.

Two definitions matter when reading counts:

* **dropped requests** are requests the pipeline deliberately never sent: a post
  deferred by an active cooldown (one unsent publish), plus retries skipped
  because the declared provider window exceeded the retry budget
  (`retries_skipped_inside_declared_window`). They are the safe direction — a
  deferral, never a duplicate.
* **posts lost** must be `0`. Every post in the matrix is either published,
  paused with a cooldown, deferred, or failed with a code; anything else is
  `unaccounted` and fails the run.

`reproducibility.counts_sha256` holds the digest of the counts projection for
each run, and `identical` says whether they match. Observed backoff seconds are
reported per scenario but excluded from the digest: the retry ladder adds
jitter, so those numbers vary between runs while every count stays fixed.

## What it does not prove

The matrix proves the **provider-status** path: the routes that observe an HTTP
status code. TikTok `publish_mode: browser` (what this host is configured for)
drives TikTok Studio through Playwright and has no provider status surface at
all, so no 429 can reach the cooldown machine on that route — by construction,
not by omission. The browser route's own safety property (an unconfirmed
failure blocks the retry instead of duplicating the post) is pinned separately in
`tests/test_rate_limit_failure_injection.py`.

It is also not a substitute for a live post. It is the counted half of the
evidence; a supervised live post under the `[xPST-TEST]` protocol remains the
other half.

## Tests

`tests/test_rate_limit_dry_run.py` pins the properties that make the counts
usable: two runs produce one digest, a second invocation in the same scratch
root is still clean, nothing opens a socket, no post is unaccounted for, and the
gate rejects a cooldown that ignores the provider's declared window.
