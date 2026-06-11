# Account Scope Decision (G54)

**Decision (v1.x): one account per platform.** Recorded 2026-06-11 so state
keys do not fossilize by accident. Owner sign-off pending (this document IS
the deliverable for ship week; the implementation choice is already true in
code).

## What is true today

Config (`config.py`), state keys (`state.json` `posted_videos` /
`content_hashes`), session storage (`credentials/<platform>_*.json`), quota
tracking, and anti-bot limits all assume exactly one account per platform.
Nothing namespaces by account id.

## Why decide now

Retrofitting multi-account later requires re-keying state
(`platform:post_id` → `platform:account:post_id`), per-account session
files, and per-account quotas. If v1 ships with ambiguous semantics, every
state file in the wild becomes a migration liability. Declaring
single-account now makes the future migration a clean, versioned schema bump
instead of a guess about which account old records belong to.

## v2 path (roadmap, not ship-week)

1. `accounts.<platform>` becomes a list with a `default` marker.
2. State schema v2: composite keys gain an account segment; migration maps
   v1 records to the default account.
3. Sessions move to `credentials/<platform>/<account>/...`.
4. Quota + anti-bot limits become per-account.

## User-facing statement (mirrored in README)

xPST v1 manages one account per platform. Creators with brand + personal
accounts should run two xPST configs (`XPST_HOME=~/.xpst-brand xpst ...`)
until multi-account lands.
