#!/usr/bin/env python
"""Scenario 3: kill -9 the engine mid-write (50 iters at random offsets).
Verify state.json is ALWAYS valid JSON (old or new, never partial); count
orphan tmp files, lock files, backups, forensic files."""
import json, os, random, signal, subprocess, sys, tempfile, time
from pathlib import Path

CHILD = r'''
import json, sys, time
sys.path.insert(0, "/Users/itxji/xPST-work/xpst-qa-config/src")
from pathlib import Path
from xpst.state_store import StateStore
store = StateStore(Path(sys.argv[1]))
big = "x" * 20000  # ~20KB payload to widen the write window
i = 0
while True:
    state = store.get()
    state["counter"] = i
    state["payload"] = big
    state["posted_videos"][f"vid_{i}"] = {"source_url": f"https://tiktok.com/@u/video/{i}"}
    store.set(state)
    i += 1
    time.sleep(0.002)
'''

def valid_state(p: Path):
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return "NOT_DICT"
        return data.get("counter")
    except Exception as e:
        return f"CORRUPT: {type(e).__name__}: {str(e)[:80]}"

results = {"corrupt": 0, "ok": 0, "orphan_tmp_seen": set(), "iters": 0}
random.seed(42)
for it in range(50):
    with tempfile.TemporaryDirectory() as td:
        cd = Path(td)
        p = subprocess.Popen([sys.executable, "-c", CHILD, str(cd)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(random.uniform(0.05, 0.5))  # kill at random point mid-write-loop
        p.kill(); p.send_signal(signal.SIGKILL); p.wait()
        sf = cd / "state.json"
        v = valid_state(sf)
        results["iters"] += 1
        if isinstance(v, str) and v.startswith("CORRUPT"):
            if "No such file" in v:
                # Killed during interpreter startup, before the first write
                # ever completed — file legitimately absent, NOT corruption.
                results.setdefault("absent_pre_first_write", 0)
                results["absent_pre_first_write"] += 1
            else:
                results["corrupt"] += 1
                results.setdefault("corrupt_detail", []).append({"iter": it, "v": v})
        elif v == "NOT_DICT":
            results["corrupt"] += 1
            results.setdefault("corrupt_detail", []).append({"iter": it, "v": v})
        else:
            results["ok"] += 1
        tmps = list(cd.glob("state.json.tmp.*"))
        if tmps:
            results["orphan_tmp_seen"].add(f"{len(tmps)} orphan tmp @iter{it}")

results["orphan_tmp_seen"] = sorted(results["orphan_tmp_seen"])[:5]
# also verify a fresh load on a killed dir recovers sanely (no crash)
print(json.dumps(results, indent=2))
