"""Windows portability contract for the resumable setup transaction store."""

from pathlib import Path

import pytest

import xpst.setup_transaction as setup_transaction_module
from xpst.setup_transaction import SetupTransactionStore


def test_atomic_write_succeeds_without_os_fchmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows Python has no ``os.fchmod``.

    The atomic write must still publish the payload, close the file
    descriptor, and leave no temporary file behind. Unconditional use of
    ``os.fchmod`` previously raised ``AttributeError`` on Windows runners and
    took every setup/onboarding test down with it.
    """
    monkeypatch.delattr(setup_transaction_module.os, "fchmod", raising=False)

    store = SetupTransactionStore(tmp_path)
    store._atomic_write_bytes(store.path, b"{}")

    assert store.path.read_bytes() == b"{}"
    leftovers = sorted(p.name for p in tmp_path.iterdir() if p.name != store.path.name)
    assert leftovers == [], f"atomic write leaked temporary files: {leftovers}"


def test_atomic_write_tightens_file_mode_when_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POSIX keeps the 0600 permission tightening; the guard must not drop it."""
    if not hasattr(__import__("os"), "fchmod"):
        pytest.skip("platform has no os.fchmod")

    calls: list[tuple[int, int]] = []
    real_fchmod = setup_transaction_module.os.fchmod

    def recording_fchmod(fd: int, mode: int) -> None:
        calls.append((fd, mode))
        real_fchmod(fd, mode)

    monkeypatch.setattr(setup_transaction_module.os, "fchmod", recording_fchmod)

    store = SetupTransactionStore(tmp_path)
    store._atomic_write_bytes(store.path, b"{}")

    assert calls, "os.fchmod was not applied where the platform supports it"
    assert all(mode == 0o600 for _, mode in calls)
