#!/usr/bin/env python
"""Scenario 7: path edge cases — spaces, unicode, symlinked config dir,
read-only dir (state + credentials + migration)."""
import json, os, stat, sys, tempfile
from pathlib import Path
sys.path.insert(0, "/Users/itxji/xPST-work/xpst-qa-config/src")
import yaml
from xpst.state_store import StateStore
from xpst.utils.credentials import CredentialStore
from xpst.config_migration import auto_migrate

out = {}

def state_roundtrip(cd: Path):
    s = StateStore(cd)
    s.update(lambda st: {**st, "posted_videos": {**st["posted_videos"], "vid✓": {"source_url": "https://x/1"}}})
    s2 = StateStore(cd)
    return "vid✓" in s2.get().get("posted_videos", {})

def cred_roundtrip(cd: Path):
    c = CredentialStore(config_dir=str(cd))
    c.store("youtube_token", "tok✓-ü")
    c2 = CredentialStore(config_dir=str(cd))
    return c2.retrieve("youtube_token") == "tok✓-ü"

base = Path(tempfile.mkdtemp())

# 1. spaces in path
p = base / "my xpst config"
p.mkdir()
out["spaces"] = {"state": state_roundtrip(p), "creds": cred_roundtrip(p)}

# 2. unicode in path
p = base / "config-ünïcødé-✓"
p.mkdir()
out["unicode"] = {"state": state_roundtrip(p), "creds": cred_roundtrip(p)}

# 3. symlinked config dir
real = base / "real"; real.mkdir()
link = base / "link"
link.symlink_to(real)
out["symlink"] = {"state": state_roundtrip(link), "creds": cred_roundtrip(link)}

# 4. read-only config dir: write must fail GRACEFULLY (exception w/ clear msg), not corrupt
ro = base / "readonly"; ro.mkdir()
StateStore(ro).update(lambda st: {**st, "marker": 1})
os.chmod(ro, stat.S_IRUSR | stat.S_IXUSR)  # r-x: no write
try:
    s = StateStore(ro)
    s.update(lambda st: {**st, "marker": 2})
    out["readonly_state"] = {"no_error": True, "in_memory_changed": s.get().get("marker")}
except Exception as e:
    out["readonly_state"] = {"error": f"{type(e).__name__}: {str(e)[:100]}"}
finally:
    os.chmod(ro, 0o755)
# after failure, is on-disk state still the ORIGINAL valid one?
out["readonly_state_after"] = json.loads((ro / "state.json").read_text()).get("marker")

# read-only creds dir
roc = base / "readonly_creds"; roc.mkdir()
c = CredentialStore(config_dir=str(roc))
os.chmod(roc, stat.S_IRUSR | stat.S_IXUSR)
try:
    c.store("k", "v")
    out["readonly_creds"] = {"no_error": True}
except Exception as e:
    out["readonly_creds"] = {"error": f"{type(e).__name__}: {str(e)[:100]}"}
finally:
    os.chmod(roc, 0o755)

# 5. migration with spaces+unicode path
for name in ("dir with spaces", "dïr-✓"):
    p = base / name; p.mkdir()
    (p / "config.yaml").write_text(yaml.safe_dump({"version": 2, "custom_flag": "keep"}))
    ok, msg = auto_migrate(p)
    d = yaml.safe_load((p / "config.yaml").read_text())
    out[f"migrate_{name.replace(' ', '_')}"] = {"ok": ok, "custom_kept": d.get("custom_flag") == "keep", "version": d.get("version")}

print(json.dumps(out, indent=2, ensure_ascii=False))
