#!/usr/bin/env python
"""Scenario 4: concurrent writers — 3 processes on same config dir, 100 total ops.
Each process adds its OWN distinct keys; at the end every key from every writer
must be present (zero lost updates)."""
import json, subprocess, sys, tempfile, time
from pathlib import Path

CHILD = r'''
import sys, time
sys.path.insert(0, "/Users/itxji/xPST-work/xpst-qa-config/src")
from pathlib import Path
from xpst.state_store import StateStore
tag = sys.argv[2]
store = StateStore(Path(sys.argv[1]))
for i in range(30):
    def up(s):
        s.setdefault("seen", {})
        s["seen"][f"{tag}_{i}"] = True
        return s
    store.update(up)
    time.sleep(0.001)
print(f"{tag} done", flush=True)
'''

with tempfile.TemporaryDirectory() as td:
    procs = [subprocess.Popen([sys.executable, "-c", CHILD, td, t],
             stdout=subprocess.PIPE, stderr=subprocess.PIPE) for t in ("A", "B", "C")]
    outs = []
    for p in procs:
        o, e = p.communicate(timeout=120)
        outs.append((p.returncode, o.decode()[-200:], e.decode()[-400:] if e else ""))
    sys.path.insert(0, "/Users/itxji/xPST-work/xpst-qa-config/src")
    from xpst.state_store import StateStore
    final = StateStore(Path(td)).get()
    seen = final.get("seen", {})
    expected = {f"{t}_{i}" for t in ("A", "B", "C") for i in range(30)}
    missing = sorted(expected - set(seen))
    print(json.dumps({
        "total_expected_keys": len(expected),
        "keys_present": len(seen),
        "LOST_UPDATES": len(missing),
        "missing_sample": missing[:10],
        "proc_results": outs,
    }, indent=2))
