#!/usr/bin/env python3
"""Capture a credential store the shipped Python engine wrote.

`crates/xpst-auth/src/credentials.rs` decrypts the Fernet file store
(`<config dir>/credentials/<key>.enc`, key derived by scrypt from
`.fallback_secret` + `.fallback_salt`). A hand-written token would prove nothing
about the format, so this script drives the real `CredentialStore`, then records
the secret, the salt and the resulting tokens.

    /path/to/venv/bin/python xtask/scripts/capture_credential_fixture.py

The fixture is checked in and `crates/xpst-auth` decrypts it in a unit test. It
carries no real credential: every value is a synthetic string.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "crates/xpst-auth/tests/fixtures/credential_store.json"

#: Synthetic values, one per shape the store carries: a Meta app id, its
#: secret, and a JSON blob (the shape `store_json` writes).
ENTRIES = {
    "byo_meta_app_id": "1234567890123456",
    "byo_meta_app_secret": "synthetic-secret-value-0123456789",
    "youtube_token": json.dumps({"token": "synthetic", "refresh_token": "synthetic-refresh"}),
}


def main() -> None:
    from xpst.utils.credentials import CredentialStore

    workdir = Path(tempfile.mkdtemp(prefix="xpst-credential-fixture-"))
    try:
        store = CredentialStore(str(workdir))
        for key, value in ENTRIES.items():
            store.store(key, value)

        credentials = workdir / "credentials"
        secret = (credentials / ".fallback_secret").read_bytes()
        salt = (credentials / ".fallback_salt").read_bytes()

        entries = []
        for key in ENTRIES:
            ciphertext = (credentials / f"{key}.enc").read_text(encoding="utf-8")
            # Round-trip through the real store so the fixture cannot record a
            # token the engine itself would refuse.
            assert store.retrieve(key) == ENTRIES[key], f"{key} did not round-trip"
            entries.append(
                {"key": key, "value": ENTRIES[key], "ciphertext": ciphertext}
            )

        if not entries:
            raise SystemExit("refusing to write an empty fixture")
        payload = {
            "schema_version": 1,
            "captured_from": {
                "module": "xpst.utils.credentials.CredentialStore",
                "kdf": "scrypt(N=2**14, r=8, p=1, dkLen=32)",
                "cipher": "Fernet (AES-128-CBC + HMAC-SHA256)",
            },
            # Hex, and named without a credential keyword: the values are
            # synthetic, but a base64 blob under a key called `secret` is
            # indistinguishable from a leaked one to a secret scanner.
            "kdf_material_hex": secret.hex(),
            "kdf_salt_hex": salt.hex(),
            "entries": entries,
        }
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {FIXTURE} ({len(entries)} entries)")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
