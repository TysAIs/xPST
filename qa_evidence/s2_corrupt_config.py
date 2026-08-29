#!/usr/bin/env python
"""Scenario 2: corrupted config.yaml — truncated, binary, empty, billion laughs, wrong types."""
import json, resource, sys, tempfile, signal
from pathlib import Path
sys.path.insert(0, "/Users/itxji/xPST-work/xpst-qa-config/src")
import yaml
from xpst.config_migration import auto_migrate


class AlarmTimeout(Exception):
    pass


def _alarm(sig, frame):
    raise AlarmTimeout("HUNG > 10s (billion laughs not rejected)")


EVID = Path("/Users/itxji/xPST-work/xpst-qa-config/qa_evidence")
VALID = """
version: 4
accounts:
  youtube:
    enabled: true
    api_key: YTKEY123
custom_flag: keep-me
"""

def run_case(name, content_bytes, timeout=8):
    with tempfile.TemporaryDirectory() as td:
        cd = Path(td); cfg = cd / "config.yaml"
        cfg.write_bytes(content_bytes)
        original = cfg.read_bytes()
        signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(10)
        try:
            ok, msg = auto_migrate(cd)   # with alarm timeout in case of hang
        except AlarmTimeout as e:
            ok, msg = "HUNG", str(e)
        except Exception as e:
            ok, msg = "EXC", f"{type(e).__name__}: {e}"
        finally:
            signal.alarm(0)
        after = cfg.read_bytes()
        backups = sorted((cd / "backups").glob("*"))
        r = {
            "result": (str(ok), str(msg)[:120]),
            "original_preserved": after == original,
            "file_size_after": len(after),
            "backups_created": [b.name for b in backups],
        }
        # then: how does the app-level loader react? (XPSTConfig from xpst.config)
        from xpst.config import XPSTConfig
        try:
            signal.alarm(10)
            c = XPSTConfig.load(str(cfg))
            r["app_load"] = "OK (loaded)"
        except AlarmTimeout as e:
            r["app_load"] = f"HUNG: {e}"
        except Exception as e:
            r["app_load"] = f"{type(e).__name__}: {str(e)[:140]}"
        finally:
            signal.alarm(0)
        return r

cases = {
    "truncated_yaml": b"version: 4\naccounts:\n  youtube:\n    enab",
    "binary_garbage": bytes(range(256)) * 4,
    "empty_file": b"",
    "billion_laughs": b"""a0: &a0 ["x","x","x","x","x","x","x","x","x"]
a1: &a1 [*a0,*a0,*a0,*a0,*a0,*a0,*a0,*a0,*a0]
a2: &a2 [*a1,*a1,*a1,*a1,*a1,*a1,*a1,*a1,*a1]
a3: &a3 [*a2,*a2,*a2,*a2,*a2,*a2,*a2,*a2,*a2]
a4: &a4 [*a3,*a3,*a3,*a3,*a3,*a3,*a3,*a3,*a3]
a5: &a5 [*a4,*a4,*a4,*a4,*a4,*a4,*a4,*a4,*a4]
a6: [*a5,*a5,*a5,*a5,*a5,*a5,*a5,*a5,*a5]
""",
    "list_root": b"- just\n- a\n- list\n",
    "string_root": b"just a string\n",
    "null_root": b"null\n",
    "wrong_type_accounts": b"version: 4\naccounts: not_a_dict\n",
    "version_string": b"version: \"three\"\naccounts: {youtube: {}}\n",
}
out = {}
for name, content in cases.items():
    out[name] = run_case(name, content)
print(json.dumps(out, indent=2))
