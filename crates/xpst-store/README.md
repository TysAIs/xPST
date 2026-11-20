# xpst-store — the SQLite ledger

`analytics.db` is the product's only database besides the knowledge vector
store. That file is what this crate implements: per-post metric snapshots,
follower counts, cross-post groups and per-platform collection outcomes, using
`rusqlite` with the `bundled` feature (no system libsqlite3, so the shipped
binary stays self-contained).

Schema authority is the Python engine — `src/xpst/analytics_store.py` — not this
crate. Every statement below is copied verbatim from it; see `src/schema.rs`.

Out of scope for this slice: the knowledge vector store, the analytics
collectors, HTTP and the CLI.

## Schema

| Object | Columns | Constraint |
| --- | --- | --- |
| `metric_snapshots` | `platform TEXT NOT NULL`, `post_id TEXT NOT NULL`, `captured_at TEXT NOT NULL`, `views`, `likes`, `comments`, `shares`, `reposts`, `saves` (all `INTEGER`), `extra TEXT`, `collection_status TEXT`, `collection_error TEXT` | `PRIMARY KEY (platform, post_id, captured_at)` |
| `idx_snapshots_post` | `(platform, post_id)` | non-unique index |
| `cross_post_groups` | `content_hash TEXT`, `video_id TEXT NOT NULL`, `caption TEXT`, `source_url TEXT`, `created_at TEXT NOT NULL`, `platforms_json TEXT NOT NULL` | `PRIMARY KEY (content_hash)` |
| `analytics_collection_runs` | `platform TEXT NOT NULL`, `captured_at TEXT NOT NULL`, `status TEXT NOT NULL`, `error`, `reason`, `detail`, `attempted`, `failures`, `requested`, `collected` | `PRIMARY KEY (platform, captured_at)` |
| `idx_collection_runs_platform` | `(platform, captured_at)` | non-unique index |
| `follower_snapshots` | `platform TEXT NOT NULL`, `count INTEGER NOT NULL`, `captured_at TEXT NOT NULL` | `PRIMARY KEY (platform, captured_at)` |

Conventions that the schema encodes and the code preserves:

* Timestamps are ISO-8601 text (`captured_at`), never epoch integers; ordering
  is lexicographic on that text.
* A metric column may be `NULL`: that is "the platform did not report it", not
  zero. Reads surface `Option<i64>`, never a fabricated `0`.
* `collection_status` is normalised on write and on read. Anything unrecognised
  — including the `NULL` left by a row written before the column existed — reads
  back as `unknown`, never `ok`.
* `extra` holds platform-specific fields as caller-supplied JSON. The store
  treats it as opaque text.
* No triggers, no foreign keys, no views, no `AUTOINCREMENT`. The vector store's
  `vec0` virtual table is a separate database and not part of this schema.

Parity is proven, not asserted: `tests/schema_parity.rs` drives the real
`xpst.analytics_store.AnalyticsStore`, then compares `sqlite_master` text,
`PRAGMA table_info`, index lists and unique constraints in both directions. It
also has Python migrate a legacy ledger and Rust migrate a byte-identical copy,
then diffs the two results.

## Migration policy

Additive and introspective, matching `AnalyticsStore._migrate`:

1. On every open, run the `CREATE ... IF NOT EXISTS` statements. Existing tables
   are left untouched.
2. Read `PRAGMA table_info(metric_snapshots)` and run the guarded
   `ALTER TABLE metric_snapshots ADD COLUMN collection_status TEXT` /
   `... collection_error TEXT` only for columns that are missing.
3. Nothing else. No table rewrite, no data movement, no drops, no reordering.
   Columns are only ever appended, so a legacy row keeps its position and its
   values; its new status columns are `NULL`, which reads as `unknown`.

There is deliberately **no schema-version counter**. Python never writes one, so
a `user_version` set by Rust would go stale as soon as Python created or migrated
the file, and the number would claim a state the database might not be in.
Introspection asks the database what it actually has.

The policy has these testable consequences (`tests/migrations.rs`):

* An older ledger upgrades without losing rows, and its values are unchanged.
* A second open runs no DDL: `PRAGMA schema_version` and `sqlite_master` are
  identical before and after.
* A *newer* database (columns this build does not know about) still opens and
  keeps them, because every read names its columns and every write names its
  columns — unknown columns keep their values or their defaults.
* A concurrent second process that loses the upgrade race is not an error: the
  duplicate-column failure is re-checked and tolerated.

## Journal mode / WAL

**Default: keep SQLite's rollback journal.** Python opens this file with the
default `journal_mode` (`delete`) and a 10 s busy timeout, and nothing here
changes that. Rationale:

* Journal mode is a persistent property of the file. Switching to WAL rewrites
  the database header, creates `-wal`/`-shm` siblings and makes a plain file copy
  an unsafe backup — a surprising side effect for a store that currently shares
  the file with the Python engine.
* WAL does not work on network filesystems, and the operator's config directory
  is not guaranteed to be local.
* The workload does not need it: snapshots are append-only, writes are single
  short transactions, and the purge is one bounded `DELETE`.
* Contention is handled where SQLite intends — a 10 s `busy_timeout` on every
  connection, the same value as Python's `timeout=10`.

`JournalMode::Wal` is available via `Store::open_with` for the point where the
Rust engine is the only writer; it is opt-in, and passing `JournalMode::Preserve`
(the default) never downgrades a database that is already in WAL.
`tests/concurrency.rs` runs four connections writing at once, plus two appending
to the same post, under the default mode, and asserts every row survives.

## Ownership gate

Python refuses to persist a snapshot it cannot attribute to the account:
YouTube ids are verified against the authenticated channel's uploads playlist,
X/Instagram/TikTok/Threads against the ids recorded in `state.json`. Failures
drop the row (fail closed); the stale-row purge only runs for a verified,
non-empty id set.

The store-level API is `ownership::OwnershipGate` (the admission decision,
returning `Owned` / `NotOwned` / `Unverifiable` / `UngatedPlatform` with the same
reason strings Python logs), `Store::record_snapshots_gated` (the
persistence-boundary filter, with per-reason counts) and
`Store::delete_snapshots_not_owned` (the purge, refusing an empty set so
incomplete evidence can never delete a platform's history).

## Running the tests

```bash
cargo test -p xpst-store
```

The four cross-language tests need an interpreter that can
`import xpst.analytics_store`. Point `XPST_PYTHON` at one:

```bash
XPST_PYTHON=/path/to/venv/bin/python cargo test -p xpst-store
```

Without such an interpreter those four tests print a `SKIP ...` line and pass;
the rest of the crate's tests are pure Rust and always run.
