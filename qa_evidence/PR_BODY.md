## Adversarial QA: config migration / credential security / state atomicity

Every scenario was executed **live** with real files, real `kill -9`, and a real 3MB loopback disk image. Evidence (before/after outputs per scenario) in `qa_evidence/`, regression tests in `tests/test_adversarial_data_loss.py` (19 tests).

### Critical bugs found & fixed

**1. Concurrent writers silently lost 67% of updates** (`src/xpst/state_store.py`)
3 processes x 30 updates on one config dir -> **60/90 updates lost**. `update()` applied the updater to a stale in-memory snapshot; the file lock protected writes but not the read-modify-write cycle. Two engines + CLI on one install = scheduled-post records vanishing.
*Fix*: updater now applies to the freshest state - re-read from disk under the cross-process lock when the file changed since our last load/write (mtime_ns+size stat signature), preserving in-memory mutations from throttled-save flows. **After: 0/90 lost** (verified 3x).

**2. Disk-full destroyed existing credentials** (`src/xpst/utils/credentials.py`)
`_write_secret_file` used `O_TRUNC`: a failed write left `youtube_token.enc` at **0 bytes** (verified on a full loopback volume - old credential gone).
*Fix*: atomic write (O_EXCL tmp + full-write loop + fsync + rename). After: failed write leaves the old `.enc` **byte-identical**, zero tmp files.

**3. Corrupt config silently reset to defaults** (`src/xpst/config_migration.py`, `src/xpst/config.py`)
Empty / `null` / non-xPST YAML configs were treated as v1 and **overwritten with generated defaults**; binary garbage crashed with a raw `UnicodeDecodeError`; unparseable YAML was silently skipped ("No migration needed").
*Fix*: migration + loader now refuse to reset - clear actionable `ValueError` ("...backed up to: ..., fix or restore, then retry"), original file **never modified**, corrupt copy saved to `backups/config.yaml.corrupt_*`. Migration writes are atomic (tmp+rename+fsync) so a crash/ENOSPC mid-migration can't truncate config.yaml (verified: original survives).

### Hardening
- `StateStore._atomic_write`: fsync before rename + dir fsync; temp file cleaned up on **any** failure (disk-full previously leaked `state.json.tmp.*`)
- Stale crash-orphaned `state.json.tmp.*` swept on startup (600s staleness guard, safe vs concurrent writers)
- YAML alias-bomb guard (billion-laughs): bounded SafeLoader caps alias expansions; live test confirmed PyYAML load is reference-graph based (no hang), but the guard + refuse-to-migrate policy blocks the dump/traversal blowup vector
- Undecryptable credentials now log **"Re-authentication required"** instead of silent `None` (wrong key / truncated file / rotated secret all verified: no crash, no leak)

### Verified-clean (no bugs)
- Migration chain v1->v2->v3->v4: platform creds, unknown/forward-compat keys preserved; idempotent (2nd run = byte-identical); plaintext `dashboard_password` hashed; backup rotation (10)
- kill -9 x 50 at random offsets mid-write: **0 partial corruption** (temp+rename holds); backups capped at 5
- Credentials at rest: Fernet ciphertext only, 0600 perms, no plaintext in logs
- Path edge cases: spaces/unicode/symlinked config dirs roundtrip fine; read-only dirs fail gracefully with original data intact

### Test changes
- New: `tests/test_adversarial_data_loss.py` (19 regression tests)
- Updated 8 existing stress/edge/cross-platform tests that codified the **old** contract (raw `YAMLError` crash, empty-file->silent defaults) to the new data-loss-prevention contract (clear `ValueError` + backup) - rationale in test docstrings

**CI-relevant result: full suite 1791 passed, 2 skipped, 0 failed** (local, venv Python 3.11).
