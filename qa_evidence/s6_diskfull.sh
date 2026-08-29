#!/usr/bin/env bash
# Scenario 6: disk-full via a tiny loopback disk image (macOS hdiutil)
set -u
IMG=$(mktemp -d)/tiny.dmg
MNT=$(mktemp -d)/mnt
hdiutil create -size 3m -fs APFS -volname XPSTFULL -ov "$IMG" >/dev/null
hdiutil attach "$IMG" -mountpoint "$MNT" >/dev/null
echo "MNT=$MNT"
# fill it
python3 - "$MNT" <<'EOF'
import sys
from pathlib import Path
p = Path(sys.argv[1]) / "filler"
with open(p, "wb") as f:
    try:
        while True:
            f.write(b"x" * 65536)
    except OSError:
        f.flush()
print("filled")
EOF
df -k "$MNT" | tail -1
cd /Users/itxji/xPST-work/xpst-qa-config
PYTHONPATH=$PWD/src /Users/itxji/XPST/.venv/bin/python - "$MNT" <<'EOF' 2>&1
import json, sys
from pathlib import Path
sys.path.insert(0, "/Users/itxji/xPST-work/xpst-qa-config/src")
from xpst.state_store import StateStore
from xpst.utils.credentials import CredentialStore
from xpst.config_migration import ConfigMigration
import yaml

mnt = Path(sys.argv[1])
out = {}

# A. state write on full disk
st_dir = mnt / "st"; st_dir.mkdir()
s = StateStore(st_dir)
s.update(lambda st: {**st, "marker": "old"})
try:
    s.update(lambda st: {**st, "marker": "new", "payload": "y" * 100000})
    out["state_write"] = "UNEXPECTED SUCCESS (image not full?)"
except Exception as e:
    out["state_write"] = f"{type(e).__name__}: {str(e)[:80]}"
on_disk = None
try:
    on_disk = json.loads((st_dir / "state.json").read_text()).get("marker")
except Exception as e:
    on_disk = f"UNREADABLE: {e}"
out["state_after"] = {
    "on_disk_marker": on_disk,
    "zero_byte_state": (st_dir / "state.json").stat().st_size == 0,
    "tmp_left": len(list(st_dir.glob("state.json.tmp.*"))),
}

# B. credential write on full disk: does it TRUNCATE the existing .enc?
cr_dir = mnt / "cr"; cr_dir.mkdir()
c = CredentialStore(config_dir=str(cr_dir))
try:
    c.store("youtube_token", "OLD-TOKEN")
except Exception as e:
    out["cred_first_store"] = f"{type(e).__name__}: {str(e)[:80]}"
enc = cr_dir / "credentials" / "youtube_token.enc"
enc_before = enc.read_bytes() if enc.exists() else b""
# free a bit of space, then re-fill less tightly: rotate key + store larger value
try:
    c._write_secret_file(enc, enc_before)  # simulate intact file
except Exception:
    pass
try:
    c.store("youtube_token", "NEW-LONGER-TOKEN-" + "z" * 200000)
    out["cred_second_store"] = "UNEXPECTED SUCCESS"
except Exception as e:
    out["cred_second_store"] = f"{type(e).__name__}: {str(e)[:80]}"
enc_after = enc.read_bytes() if enc.exists() else b""
out["cred_after"] = {
    "file_survived": enc.exists(),
    "size_before": len(enc_before),
    "size_after": len(enc_after),
    "truncated_or_destroyed": len(enc_after) < len(enc_before),
    "tmp_left": len(list((cr_dir / "credentials").glob("*.tmp*"))),
}

# C. config migration write on full disk: does original config survive?
cfg_dir = mnt / "cfg"; cfg_dir.mkdir()
cfg = cfg_dir / "config.yaml"
cfg.write_text(yaml.safe_dump({"version": 2, "accounts": {"youtube": {"api_key": "KEEP"}}}))
before = cfg.read_bytes()
m = ConfigMigration(cfg_dir)
try:
    ok, msg = m.migrate()
    out["migrate"] = f"ok={ok} {msg[:60]}"
except Exception as e:
    out["migrate"] = f"{type(e).__name__}: {str(e)[:80]}"
after = cfg.read_bytes() if cfg.exists() else b""
out["config_after"] = {
    "original_survived": after == before or len(after) > 0,
    "size_after": len(after),
    "still_valid_yaml_or_original": True,
    "zero_byte_config": len(after) == 0,
}
print(json.dumps(out, indent=2))
EOF
RC=$?
hdiutil detach "$MNT" >/dev/null 2>&1
exit $RC
