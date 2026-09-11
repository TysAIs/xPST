"""Regression tests for Windows-compatible setup transaction persistence."""

from pathlib import Path

import pytest

from xpst.setup_transaction import SetupTransactionStore


def test_atomic_write_succeeds_when_fchmod_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows Python has no os.fchmod; persistence must still close the fd."""
    monkeypatch.delattr("xpst.setup_transaction.os.fchmod", raising=False)

    store = SetupTransactionStore(tmp_path)
    store._atomic_write_bytes(store.path, b"{}")

    assert store.path.read_bytes() == b"{}"
    assert not list(tmp_path.glob("*.tmp"))
