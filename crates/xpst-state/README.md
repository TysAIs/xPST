# xpst-state — the record of what was published

`state.json` in the config directory is xPST's memory of every published post:
which video, on which platform, under which platform-side id, and what has
happened to it since. This crate is the typed model of that document plus the
operations and the storage rules around it.

It is not a cache and it is not a report. The publishing path reads it before
every upload to decide whether it has already put this file on this platform, so
a defect here does not show up as a wrong number — it shows up as the same video
published twice, or as a post the operator believes is gone.

Out of scope for this slice: the post pipeline, providers, HTTP.

## Journal documents

The same atomicity and locking this crate gives `state.json` is available to
side documents through `journal::JsonDocument`: one JSON value per file, a
temp file + `fsync` + `rename` write, and a read-modify-write that runs under an
exclusive advisory lock on `<file>.lock` so two processes cannot apply a change
to the same stale snapshot. `xpst-pipeline` uses it for the per-plan record
(`plans/<plan-id>.json`): that is what makes a confirmed plan survive a restart
and stops a second process from publishing a destination the first already
published. The document's contents are opaque to this crate — it owns
durability, the pipeline owns the shape.

## The on-disk shape

Shape authority is the Python engine (`src/xpst/state_manager.py`,
`src/xpst/state_store.py`). This is the document both implementations write:

```json
{
  "version": 2,
  "posted_videos": {
    "<video_id>": {
      "source_url": "https://example.invalid/src/a",
      "source_platform": "local",
      "caption": "",
      "posted_to": {
        "youtube": {
          "id": "yt-a",
          "url": "https://example.invalid/v/yt-a",
          "timestamp": "2026-01-02T03:04:05.000001"
        }
      },
      "downloaded_at": "2026-01-02T03:04:05.000001",
      "last_attempt": "2026-01-02T03:04:05.000001",
      "content_hash": "sha256:...",
      "errors":         {"<platform>": {"error": "", "timestamp": "", "count": 1}},
      "failed_attempts": {"<platform>": {"error": "", "timestamp": "", "count": 1}}
    }
  },
  "content_hashes": {"sha256:...": "<video_id>"},
  "health": {
    "platforms": {
      "<platform>": {"status": "ok", "last_success": null, "failures": 0}
    },
    "sources": {},
    "total_processed": 0,
    "last_check": null,
    "last_wake_check": null
  }
}
```

Notes that are easy to get wrong:

* `errors` and `failed_attempts` only exist once a failure has been recorded, and
  they are written by the same call with the same content. Both are read by
  shipped callers.
* `health.platforms` starts with the five platforms the engine seeds
  (instagram, threads, tiktok, x, youtube). `health.sources` is per-source fetch
  health and stays empty until a source fails.
* Timestamps are naive UTC ISO-8601 with microseconds and no offset. The rest of
  the product compares them against naive `datetime.now()` values, so a `Z` or a
  `+00:00` suffix would be a behaviour change, not a cosmetic one.

## Invariants

### 1. The platform post id lives under `id`

```json
"posted_to": {"youtube": {"id": "yt-a", "url": "...", "timestamp": "..."}}
```

`post_id` is a legacy spelling. It is accepted on read, resolved after `id`, and
**never written** by this crate — and when a file arrives with only `post_id`,
that key is preserved rather than converted or dropped.

This is not tidiness. A real defect came from a reader that looked for
`post_id`: every destination recorded under `id` returned no id, so the live
analytics refresh collected nothing for months, silently. The contract test is
named for it — `the_platform_post_id_is_stored_under_id_never_post_id` — and the
Python probe rejects any destination that carries the legacy key.

### 2. Three different "not really there" states

| State | Fields written | Post alive? | Reversible? |
| --- | --- | --- | --- |
| Soft hide (reversible unpublish) | `visibility`, `soft_hidden`, `soft_hidden_at` | **yes** | yes, set visibility back |
| Unconfirmed delete | `delete_pending`, `delete_pending_at`, `delete_pending_reason`, `delete_pending_detail`, `deleted: false` | **yes** | yes, it never deleted anything |
| Hard delete (tombstone) | `deleted: true`, `deleted_at`, `deleted_reason`, `deleted_detail` | no | no, but the video may be re-posted |

A hard delete clears the pending and visibility markers on that entry, so the
file cannot claim both "removed" and "still live". A soft hide or a pending
delete keeps its `url`, so a human can act on the post directly.

Tombstones are history, not absence: the entry keeps `id`/`url`/`timestamp` so
analytics can label the row "removed from platform". `is_posted` excludes them,
which is what allows a re-post; a later successful publish replaces the entry
outright.

### 3. The content hash is the dedupe key, and it outlives a single platform

`content_hashes[hash] = video_id` is written when a video is first recorded with
that hash. Removing one destination record does not touch it — another platform
still holds that content, and clearing the hash would let the same file be
uploaded again. Only removing the **last** destination record drops the hash, and
it drops the video record with it.

### 4. Writes are atomic and mutually exclusive

* **Atomic.** Every write goes to a uniquely named temporary file in the same
  directory, is `fsync`ed, `rename`d over the target, and the directory entry is
  `fsync`ed. A crash leaves the whole old document or the whole new one.
* **Cross-process.** Every read-modify-write holds an exclusive advisory lock on
  `.state.lock` — the same path, and the same `flock` primitive, the Python
  `StateStore` uses. A Rust write and a Python write cannot interleave, and each
  mutation re-reads the document *inside* the critical section, so no writer can
  apply a change to a snapshot that another writer has already superseded.
  `tests/durability.rs` spawns four real child processes writing into one
  directory and asserts every record and every counter increment survived; it
  also has the Python engine hold `.state.lock` while a Rust write runs, and
  fails if the write does not wait — the only test that can tell a shared lock
  from two private ones.

### 5. Corruption fails closed — and this is why

A file that does not parse is an **error**. It is never replaced with an empty
document, and it is never migrated, downgraded or "recovered" from a backup by
this crate.

An empty state document is not "no information". It is the assertion that
**nothing has ever been posted**, and the next run acts on it by publishing every
video again. Refusing to proceed costs one investigation; resetting the file costs
the account. The error message says so:

```text
State file /home/user/.xpst/state.json is corrupted (...); refusing to read or write it.
An empty state file would re-post every video, so nothing was reset — restore the file
or let the Python engine recover it from backups/
```

The Python engine does rotate `backups/state_*.json` and will recover from one.
Choosing which backup to believe is a policy decision about which history is true,
and silently adopting an older document can re-post exactly as badly as an empty
one — so this crate reports the damage and leaves the file untouched. A file from
a *newer* schema version is refused for the same reason: rewriting it would drop
fields this build does not understand.

A missing file is different, and is not corruption: that is a fresh install.

## API

```rust
use xpst_state::{PostedPost, StateManager};

let mut state = StateManager::open("/path/to/config/dir")?;
let mut posted = std::collections::BTreeMap::new();
posted.insert("youtube".into(), PostedPost::new("yt-a", "https://example.invalid/v/yt-a"));

state.record_published_post("vid-1", "https://example.invalid/src", "local", &posted, "", Some("sha256:abc"))?;
state.record_failure("vid-1", "tiktok", "rate limited")?;
state.is_posted("vid-1", "youtube")?;                 // true
state.is_fully_cross_posted("vid-1", &["youtube"])?;   // tombstone-aware
state.get_by_hash("sha256:abc")?;                     // Some("vid-1")
state.set_visibility("vid-1", "youtube", "private")?;  // soft hide
state.mark_delete_pending("vid-1", "youtube", "confirmation_outstanding", None)?;
state.record_delete_tombstone("vid-1", "youtube", "hard_delete", None)?;
state.remove_post("vid-1", "youtube")?;               // drops the hash with the last platform
```

`StateFile` is the layer below (open / refresh / update / save) for callers that
need the raw document rather than the operations.

Query methods read from disk under the lock, so two managers in two processes
never disagree about whether something was already published.

## Parity with the Python engine

`tests/state_parity.rs` runs the real `xpst.state_manager.StateManager` — not a
transcription of it — and compares canonical dumps of whole documents in both
directions, through Python's own canonical JSON formatter so neither side grades
its own homework:

* **Python → Rust.** Python performs every operation; Rust reads the document and
  writes it back; the canonical dump must be byte-identical. Any field the typed
  model fails to preserve appears as a diff.
* **Rust → Python.** Rust performs the same operation sequence from scratch;
  Python's real API then asserts each lifecycle invariant against the file
  (`python_parity/state_tool.py verify`), and the normalised canonical dump must
  equal the dump of the document Python itself wrote for the same operations.

Unknown fields are preserved on both sides: every struct carries a flattened
`extra` map, so a key written by another build or by hand survives a Rust write.
Mutation testing is part of the deal — dropping a single field, or double-counting
`total_processed`, makes both directions fail.

Set `XPST_PYTHON` to an interpreter that can import the engine to run them; they
skip loudly when none is available, which is why CI's Rust-only lane passes
without the engine installed.

## Deliberate divergences from the Python engine

Named rather than hidden:

1. **No backup recovery, no automatic migration.** Python loads a corrupt file
   and silently falls back to the newest `backups/state_*.json`, or migrates a v1
   file in place. This crate refuses both and says why. A migration path is a
   separate, reviewable change; a silent fallback is the failure mode this layer
   exists to prevent. (The read-only summary in `summary.rs` still *reports* a v1
   document — it just never rewrites one.)
2. **`last_error` is carried as a raw JSON value** (`PlatformHealth::last_error()`
   / `set_last_error()`), because Python stores both `null` and an absent key and
   the difference must survive a round trip.
3. **No dead-letter queue, statistics or circuit-breaker views.** Those are read
   models over the same document and belong to whoever renders them, not to the
   state layer. The Python engine keeps its own and reads the file this crate
   writes.
