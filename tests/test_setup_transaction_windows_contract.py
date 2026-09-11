"""Cross-platform setup transaction persistence contracts."""

import os
from pathlib import Path
from unittest.mock import patch

from xpst.setup_transaction import SetupTransactionStore


def test_atomic_write_uses_windows_safe_permission_and_cleanup_paths(tmp_path: Path) -> None:
    store = SetupTransactionStore(tmp_path)
    payload = b"{}"

    with patch.object(os, "fchmod", None, create=True):
        store._atomic_write_bytes(store.path, payload)

    assert store.path.read_bytes() == payload
    assert not list(tmp_path.glob("*.tmp"))
