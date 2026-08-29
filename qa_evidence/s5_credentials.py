#!/usr/bin/env python
"""Scenario 5: credentials.enc — wrong key, truncated file, key rotation,
plaintext-leak grep of logs."""
import io, json, logging, sys, tempfile
from pathlib import Path
sys.path.insert(0, "/Users/itxji/xPST-work/xpst-qa-config/src")
from xpst.utils.credentials import CredentialStore, PlaintextStorageError

SECRET = "SUPERSECRET-TOKEN-abc123"
out = {}

def fresh():
    td = tempfile.mkdtemp()
    return CredentialStore(config_dir=td), Path(td)

# 1. roundtrip + at-rest check
cs, d = fresh()
cs.store("youtube_token", SECRET)
enc = (d / "credentials" / "youtube_token.enc").read_bytes()
out["at_rest"] = {
    "plaintext_on_disk": SECRET.encode() in enc,
    "looks_fernet": enc[:1] == b"g" and len(enc) > 100,
    "perms": oct((d / "credentials" / "youtube_token.enc").stat().st_mode & 0o777),
    "secret_file_perms": oct((d / "credentials" / ".fallback_secret").stat().st_mode & 0o777),
}
out["roundtrip"] = cs.retrieve("youtube_token") == SECRET

# 2. wrong decryption key (recreate store with different secret)
with open(d / "credentials" / ".fallback_secret", "wb") as f:
    f.write(b"WRONG-KEY" * 4)
cs2 = CredentialStore(config_dir=d)
import logging
logbuf = io.StringIO()
h = logging.StreamHandler(logbuf)
logging.getLogger("xpst.utils.credentials").addHandler(h)
logging.getLogger("xpst.utils.credentials").setLevel(logging.DEBUG)
val = cs2.retrieve("youtube_token")
out["wrong_key"] = {"returns": val, "no_crash": True, "logs": logbuf.getvalue()[-200:]}

# 3. truncated file
cs3, d3 = fresh()
cs3.store("x_token", SECRET)
p = d3 / "credentials" / "x_token.enc"
p.write_bytes(p.read_bytes()[:10])
val = cs3.retrieve("x_token")
out["truncated"] = {"returns": val, "no_crash": True}

# 4. key rotation: rotate secret+salt, old creds unreadable -> what UX?
cs4, d4 = fresh()
cs4.store("instagram_sessionid", SECRET)
(d4 / "credentials" / ".fallback_secret").unlink()
(d4 / "credentials" / ".fallback_salt").unlink()
cs5 = CredentialStore(config_dir=d4)
val = cs5.retrieve("instagram_sessionid")
out["rotation"] = {"returns": val, "no_crash": True}

# 5. plaintext leak into logs
logbuf2 = io.StringIO()
h2 = logging.StreamHandler(logbuf2)
root = logging.getLogger()
root.addHandler(h2); root.setLevel(logging.DEBUG)
cs6, d6 = fresh()
try:
    cs6.store("youtube_token", SECRET)
    cs6.retrieve("youtube_token")
    cs6.store("youtube_token", "EXC: raise for test")
except PlaintextStorageError:
    pass
out["log_leak"] = {"secret_in_logs": SECRET in logbuf2.getvalue()}

print(json.dumps(out, indent=2))
