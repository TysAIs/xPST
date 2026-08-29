#!/usr/bin/env python
"""Scenario 1: migration chain v1/v2/v3 -> v4. Data loss, idempotency, forward-compat."""
import json, sys, tempfile, copy
from pathlib import Path
sys.path.insert(0, "/Users/itxji/xPST-work/xpst-qa-config/src")
import yaml
from xpst.config_migration import ConfigMigration, auto_migrate

EVID = Path("/Users/itxji/xPST-work/xpst-qa-config/qa_evidence")

def flat_diff(a, b, path=""):
    """All leaf-level differences between two nested dicts."""
    diffs = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            diffs += flat_diff(a.get(k), b.get(k), f"{path}.{k}" if path else k)
    elif a != b:
        diffs.append((path, a, b))
    return diffs

# Historical shapes (v1 flat platform keys; sentinel customer data in each)
V1 = {
    "youtube": {"api_key": "YTKEY123", "channel_id": "UC_cust"},
    "instagram": {"sessionid": "IGSESS456"},
    "x": {"auth_token": "XTOK789"},
    "tiktok": {"sessionid": "TTSESS000"},
    "check_interval": 600,
    "downloads_dir": "~/Downloads/xpst",
    "custom_flag": "keep-me",          # unknown/forward-compat key
    "dashboard_password": "hunter2",   # plaintext pwd that v2->v3 must hash
}
V1["monitoring"] = {"dashboard_password": "hunter2"}  # per migration code it reads monitoring.dashboard_password

results = {}
for start_v in (1, 2, 3):
    base = {"version": start_v}
    if start_v == 1:
        base.update(V1); base.pop("monitoring"); base["monitoring"] = {"dashboard_password": "hunter2"}
    elif start_v == 2:
        base.update({
            "accounts": {p: dict(V1[p]) for p in ("youtube", "instagram", "x", "tiktok")},
            "monitoring": {"dashboard_password": "hunter2", "log_level": "DEBUG"},
            "schedule": {"check_interval": 600},
            "custom_flag": "keep-me",
        })
    else:  # v3
        base.update({
            "accounts": {p: dict(V1[p]) for p in ("youtube", "instagram", "x", "tiktok")},
            "monitoring": {"dashboard_password_hash": "$2b$12$existing", "log_level": "DEBUG"},
            "notifications": {"enabled": True, "discord_webhook_url": "https://discord/hook"},
            "video_processing": {"max_file_size_mb": 100},
            "schedule": {"check_interval": 600},
            "custom_flag": "keep-me",
        })
    with tempfile.TemporaryDirectory() as td:
        cd = Path(td); cfg = cd / "config.yaml"
        cfg.write_text(yaml.safe_dump(base, sort_keys=False))
        before = yaml.safe_load(cfg.read_text())
        ok1, msg1 = auto_migrate(cd)
        after1 = yaml.safe_load(cfg.read_text())
        backup_made = list((cd / "backups").glob("config.yaml.backup_*"))
        # idempotency: run again
        ok2, msg2 = auto_migrate(cd)
        after2 = yaml.safe_load(cfg.read_text())
        # unknown-key preservation
        unknown_kept = flat_diff({"custom_flag": "keep-me"}, {"custom_flag": after2.get("custom_flag")}) == []
        # sentinel checks
        sent = {
            "youtube_api_key": "YTKEY123" in json.dumps(after2),
            "ig_sessionid": "IGSESS456" in json.dumps(after2),
            "x_token": "XTOK789" in json.dumps(after2),
            "tiktok_sess": "TTSESS000" in json.dumps(after2),
            "plaintext_pwd_gone": "hunter2" not in json.dumps(after2),
            "unknown_key_kept": after2.get("custom_flag") == "keep-me",
            "check_interval_kept": str(600) in json.dumps(after2),
        }
        # check_interval: v1 had flat check_interval=600 — did migration keep it?
        results[f"v{start_v}"] = {
            "migrate1": (ok1, msg1), "migrate2_idempotent": (ok2, msg2),
            "second_run_diff": flat_diff(after1, after2)[:5],
            "backups": len(backup_made),
            "sentinels": sent,
        }
        (EVID / f"s1_v{start_v}_before.yaml").write_text(yaml.safe_dump(before, sort_keys=False))
        (EVID / f"s1_v{start_v}_after.yaml").write_text(yaml.safe_dump(after2, sort_keys=False))

print(json.dumps(results, indent=2, default=str))
