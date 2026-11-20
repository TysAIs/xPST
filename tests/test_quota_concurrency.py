"""The quota ledger is shared state (defect: concurrent writers clobber it).

``QuotaManager`` loaded ``quotas.json`` once at init, mutated it in memory and
wrote it back with a plain ``write_text``: no reload-before-save, no
cross-process lock, no atomic replace. Two processes sharing ``~/.xpst`` (the
daemon plus a CLI/MCP invocation) each wrote their own stale view, so the
last writer silently erased the other's count — and a crash mid-write left a
truncated file the loader read as "zero used", which is the direction that
burns a real platform limit.
"""

import json
from pathlib import Path

from xpst.utils.quota import QuotaManager


def _used(tmp_path: Path, platform: str) -> int:
    data = json.loads((tmp_path / "quotas.json").read_text(encoding="utf-8"))
    return int(data[platform]["used_today"])


def test_two_managers_do_not_clobber_each_others_usage(tmp_path: Path) -> None:
    """Two processes (two instances) each record one upload: the file says two."""
    first = QuotaManager(str(tmp_path))
    second = QuotaManager(str(tmp_path))

    first.record_upload("x")
    second.record_upload("x")

    assert _used(tmp_path, "x") == 2
    assert QuotaManager(str(tmp_path)).get_remaining("x")["daily"] == first.quotas["x"].daily_limit - 2


def test_record_upload_reloads_the_live_ledger(tmp_path: Path) -> None:
    """A stale in-memory view must not overwrite a newer on-disk count."""
    stale = QuotaManager(str(tmp_path))
    other = QuotaManager(str(tmp_path))
    other.record_upload("youtube")
    other.record_upload("youtube")

    stale.record_upload("youtube")

    assert _used(tmp_path, "youtube") == 3


def test_save_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    manager = QuotaManager(str(tmp_path))
    manager.save()

    leftovers = [p.name for p in tmp_path.iterdir() if ".tmp." in p.name]
    assert leftovers == []
    assert json.loads((tmp_path / "quotas.json").read_text(encoding="utf-8"))["x"]


def test_windows_lock_branch_is_exercised_without_fcntl(tmp_path: Path, monkeypatch) -> None:
    """The msvcrt path must actually lock and unlock.

    CI's windows legs cannot be reproduced here, so the branch that only runs
    on Windows is driven directly instead of being shipped untested.
    """
    import xpst.utils.quota as quota_module

    calls: list[int] = []

    class _FakeMsvcrt:
        LK_LOCK = 1
        LK_UNLCK = 2

        @staticmethod
        def locking(fd: int, mode: int, size: int) -> None:  # noqa: ANN001
            calls.append(mode)

    monkeypatch.setattr(quota_module, "fcntl", None)
    monkeypatch.setattr(quota_module, "msvcrt", _FakeMsvcrt())

    QuotaManager(str(tmp_path)).record_upload("x")

    assert calls == [_FakeMsvcrt.LK_LOCK, _FakeMsvcrt.LK_UNLCK]
    assert _used(tmp_path, "x") == 1
