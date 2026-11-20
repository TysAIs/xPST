# xpst-limits — publishing rate discipline

The policy in [`docs/technical/safe-operating-envelope.md`](../../docs/technical/safe-operating-envelope.md),
enforced as data plus a durable state machine. This is the crate that stops an
account being limited: it decides *whether*, *when* and *why not*, and it
remembers across restarts.

It contains no HTTP, no platform adapter and no CLI. A provider crate turns a
real response into a [`PlatformResponse`]; this crate turns that into state.

## The policy as data

[`envelope()`](src/envelope.rs) returns one row per platform, with the
confidence tag carried as a value rather than a comment:

| Field | Meaning |
| --- | --- |
| `min_interval` | documented minimum interval — a floor no jitter may go below |
| `interval_range_max` | upper end of the documented range, where the row has one |
| `daily_cap` | what this crate enforces: the **low** end of the documented range |
| `daily_range_max` | high end of the documented range |
| `ceiling` | the vendor's published ceiling, `None` where none is published |
| `same_asset` | risk, and whether the vendor prohibits it in writing |
| `challenge` | what a throttle/challenge must cost, and whether a human is needed |
| `jitter_spread` | 0.30–0.50, inside the policy's ±30–50 % band |

`daily_cap` and `ceiling` are never the same number and never merged. Reporting
"4 of 4" while the vendor documents 100 is the difference between an operator
thinking the product is broken and an operator knowing the cap is a choice.

## The decision

```rust
match store.claim(&request)? {
    Decision::Allow { next_due_ms } => { /* publish */ }
    Decision::Defer { until_ms, .. } => { /* requeue, do not sleep-retry */ }
    Decision::Refuse { code, rule, .. } => { /* stop, surface both */ }
}
```

Precedence is fixed, most restrictive first: state integrity → human stop →
active cooldown → same asset on another account → daily cap → minimum interval.
Every refusal carries a `RefusalCode` **and** the policy rule that produced it
(`rule-1-same-asset-across-accounts`, `rule-2-jittered-minimum-interval`,
`rule-3-challenge-hard-stop`, `per-platform-daily-cap`,
`state-integrity-fail-closed`).

Two properties that are load-bearing:

* **`claim` is atomic.** The decision and the counter increment happen inside
  one cross-process critical section, so two processes cannot both be told there
  is one slot left. `decide` is the advisory read-only variant.
* **Jitter is deterministic** for a given (platform, account, last publish). It
  is a log-normal draw, so intervals vary; it is not a fresh random number per
  call, so a DEFER does not drift while a scheduler polls it. The nominal
  interval is inflated by `1/(1-spread)` so the downward half of the band still
  lands at or above the documented floor — jitter never buys a faster cadence
  than the platform allows.

## The ledger

`rate_limits.json` under the config directory, one record per
`platform/account`: strikes, cooldown deadline, human-required flag, the rolling
24 h publish instants, and per asset the accounts that published it.

* Every write is temp-file → `fsync` → `rename`, under an advisory `flock` on
  `.rate_limits.lock` held for the whole read-modify-write.
* The ladder is `60 s → 120 → 240 → … → 24 h` (capped), ported from
  `src/xpst/utils/cooldown_store.py` and pinned by a captured fixture.
* A clean publish resets the ladder. A supplied `Retry-After` raises the pause
  above the ladder; nothing lowers it.
* Instagram, Threads and Facebook share Meta's app rate pool, so a throttle on
  one extends the others — never shortens them. The session/direct modes are
  not in that family: they have no Meta app pool, which is also how Python
  models it.
* A file that cannot be parsed is **quarantined** and replaced by a state that
  refuses every publish for 24 h, persisted so a restart cannot clear it. A file
  from a newer build is refused without being touched.

### Deliberate differences from the Python store

Both are in the safer direction and both are pinned by tests:

1. A supplied `Retry-After` longer than 24 h is honoured (up to a 7-day sanity
   bound); Python truncates it to the ladder cap.
2. A hit never shortens an already-open window; Python rewrites the deadline and
   can end a 15-minute wait after 8 minutes.

## Parity fixture

```bash
PYTHONPATH="$PWD/src" python scripts/capture_cooldown_parity.py \
    > crates/xpst-limits/tests/fixtures/cooldown_ladder.json
```

The fixture comes from executing the shipped Python module, not from reading it.
Re-running the script must reproduce it byte for byte.

## Out of scope

HTTP, platform adapters, the CLI, and the "one in-flight write per account"
half of policy rule 3 (the caller serialises its own publishes; this crate owns
the challenge half).
