# QA evidence — config migration / credential security / state atomicity

Branch: qa/config-state-adversarial. All probes executed live (real files, real kills, real loopback disk image).

| File | Scenario | Verdict before fix | After fix |
|---|---|---|---|
| s1_out.json / s1_out_fixed*.json | Migration chain v1/v2/v3→v4, idempotency, unknown keys | PASS (no data loss, idempotent, forward-compat kept) | PASS |
| s2_out.json / s2_out_fixed.json | Corrupt config (empty/null/list/binary/bomb) | BUG: silently overwritten with generated defaults; binary garbage = raw UnicodeDecodeError, no backup | FAIL→clear error, original preserved + backed up, never reset |
| s3_out.json / s3_out_fixed3.json | kill -9 mid-write x50 | No partial corruption ever (atomic rename holds); orphan tmp files accumulate. "missing file" runs = killed during interpreter startup before first write (not corruption) | **0/50 partial corruption**; stale tmps swept on init (600s guard) |
| s3b_out.json | 50 kills same dir: accumulation | backups capped 5 OK; 1 orphan tmp never cleaned | sweep added (600s staleness guard) |
| s4_out.json / s4_out_fixed*.json | 3 processes x 30 concurrent state updates | **CRITICAL: 60/90 updates LOST (67%)** | 0/90 lost |
| s5_out.json / s5_out_fixed.json | Credentials: wrong key/truncation/rotation/plaintext | No crash, no plaintext leak, 0600 perms — but silent None, no re-auth guidance | + explicit "Re-authentication required" warning log |
| s6_out.json / s6_out_fixed2.json | Disk-full (3MB loopback image) | BUG: credential .enc TRUNCATED TO 0 BYTES (O_TRUNC destroy); orphan tmp leak | old credential byte-identical, zero tmps, originals intact, graceful OSError |
| s7_out.json / s7_out_fixed.json | Spaces/unicode/symlink paths, read-only dirs | PASS (graceful PermissionError, no corruption) | PASS |

Run with: PYTHONPATH=<repo>/src ~/.venv python qa_evidence/sN_*.py
