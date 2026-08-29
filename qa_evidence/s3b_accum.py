#!/usr/bin/env python
"""Scenario 3b: 50 kill -9 rounds against THE SAME config dir — measure
orphan state.json.tmp.* / .state.lock / backups / forensic accumulation."""
import json, signal, subprocess, sys, tempfile, time, random
from pathlib import Path

CHILD = r'''
import sys, time
sys.path.insert(0, "/Users/itxji/xPST-work/xpst-qa-config/src")
from pathlib import Path
from xpst.state_store import StateStore
store = StateStore(Path(sys.argv[1]))
i = 0
while True:
    state = store.get()
    state["counter"] = i
    state["payload"] = "x" * 40000
    store.set(state)
    i += 1
    time.sleep(0.001)
'''

with tempfile.TemporaryDirectory() as td:
    cd = Path(td)
    random.seed(7)
    for it in range(50):
        p = subprocess.Popen([sys.executable, "-c", CHILD, str(cd)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(random.uniform(0.15, 0.4))
        p.kill(); p.wait()
    # load once more to see if recovery cleans up
    sys.path.insert(0, "/Users/itxji/xPST-work/xpst-qa-config/src")
    from xpst.state_store import StateStore
    s = StateStore(cd).get()
    counts = {
        "orphan_tmp_files": len(list(cd.glob("state.json.tmp.*"))),
        "lock_files": len(list(cd.glob(".state.lock"))),
        "backups": len(list((cd / "backups").glob("state_*.json"))),
        "forensic": len(list((cd / "backups").glob("corrupted_*.json"))) + len(list(cd.glob("state.json.forensic"))),
        "final_counter_present": "counter" in s,
    }
    print(json.dumps(counts, indent=2))
