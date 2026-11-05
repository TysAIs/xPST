"""Private/internal IP category for the public-safety scanner (D-2).

The project rule is zero LAN and tailnet addresses in public material, so the
scanner reports RFC1918 and CGNAT host addresses in tree and artifact mode and
reports them (never gating) in history mode.

A line whose *subject* is a private address -- the SSRF guard's fixtures are the
case in point -- opts out with the ``public-safety: allow-private-ip`` pragma.
The pragma is deliberately line-scoped: it silences the ``private_ip`` category
for that one line and nothing else, so it cannot hide a deny-list hit or a
private address on the following line. Every private literal in this module
therefore carries one, which is also why this file stays clean under the real
tree scan it asserts below.
"""

from __future__ import annotations

import json
import subprocess
import zipfile
from typing import TYPE_CHECKING

import pytest

from scripts import scan_public_safety as scanner
from scripts.scan_public_safety import (
    scan_artifact,
    scan_history,
    scan_public_safety,
    scan_text,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

PRAGMA = "public-safety: allow-private-ip"

# RFC1918 host addresses, assembled one per line so the pragma below applies to
# exactly the line that carries the literal.
RFC1918_HOSTS = [
    "10.0.0.1",  # public-safety: allow-private-ip
    "10.255.255.254",  # public-safety: allow-private-ip
    "172.16.4.4",  # public-safety: allow-private-ip
    "172.31.255.1",  # public-safety: allow-private-ip
    "192.168.1.1",  # public-safety: allow-private-ip
    "192.168.255.254",  # public-safety: allow-private-ip
]
CGNAT_HOSTS = [
    "100.64.0.1",  # public-safety: allow-private-ip
    "100.127.255.254",  # public-safety: allow-private-ip
]
# Public, loopback, link-local and out-of-range neighbours are NOT findings.
# None of these need the pragma, which is the point.
NOT_FINDINGS = [
    "8.8.8.8",
    "93.184.216.34",
    "100.100.100.200",  # Alibaba Cloud metadata: public, inside 100.64/10
    "169.254.169.254",  # link-local IMDS, not RFC1918
    "172.15.0.1",  # just below 172.16/12
    "172.32.0.1",  # just above 172.16/12
    "127.0.0.1",
    "127.1.2.3",
    "0.0.0.0",
    "255.255.255.0",
    "999.1.1.1",  # not a valid quad at all
]
HOST = RFC1918_HOSTS[0]
NET_GUARD = scanner.ROOT / "src" / "xpst" / "utils" / "net_guard.py"


# --- Detection ------------------------------------------------------------
@pytest.mark.parametrize("address", RFC1918_HOSTS + CGNAT_HOSTS)
def test_private_host_is_reported(address):
    findings = scan_text(f"host = {address}\n", "notes.md")

    assert [finding.kind for finding in findings] == ["private_ip"]


@pytest.mark.parametrize("address", NOT_FINDINGS)
def test_public_and_non_private_addresses_are_not_reported(address):
    assert scan_text(f"host = {address}\n", "notes.md") == []


@pytest.mark.parametrize("literal", ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"])
def test_cidr_network_literal_is_not_a_host_finding(literal):
    # A CIDR block declares a network; it names no particular machine.
    assert scan_text(f"LAN = ip_network({literal!r})\n", "net.py") == []


def test_url_path_is_not_mistaken_for_a_cidr():
    assert [finding.kind for finding in scan_text(f"http://{HOST}/admin\n", "x.py")] == ["private_ip"]


def test_finding_detail_never_echoes_the_address():
    findings = scan_text(f"host = {HOST}\n", "notes.md")
    payload = json.dumps([finding.to_dict() for finding in findings])

    assert HOST not in payload


# --- net_guard.py must stay clean (the real-world constraint) -------------
def test_net_guard_cgnat_constant_and_metadata_address_stay_clean():
    text = NET_GUARD.read_text(encoding="utf-8")
    # Prove the test is not vacuous: the file really does carry both literals.
    assert "100.64.0.0/10" in text
    assert "100.100.100.200" in text

    result = scan_public_safety(scanner.ROOT, [NET_GUARD])

    assert [finding for finding in result["findings"] if finding["kind"] == "private_ip"] == []


def test_net_guard_would_be_reported_without_the_cidr_rule():
    # The CGNAT network's base address used as a *host* -- nothing follows it
    # like /10 -- is a private address. The trailing pragma exempts this source
    # line from the tree scan; the string handed to scan_text carries no pragma,
    # so the finding must stand.
    findings = scan_text("bind = 100.64.0.0\n", "net.py")  # public-safety: allow-private-ip

    assert [finding.kind for finding in findings] == ["private_ip"]


# --- The pragma mechanism -------------------------------------------------
def test_pragma_on_the_line_suppresses_the_finding():
    line = f'URL = "http://{HOST}/hook"  # {PRAGMA}\n'

    assert scan_text(line, "test_ssrf_guard.py") == []


def test_pragma_without_the_literal_is_harmless():
    assert scan_text(f"# {PRAGMA}\nhost = 8.8.8.8\n", "notes.md") == []


def test_pragma_is_line_scoped():
    text = f"first {HOST}  # {PRAGMA}\nsecond {HOST}\n"

    findings = scan_text(text, "notes.md")

    assert len(findings) == 1
    assert findings[0].kind == "private_ip"


def test_pragma_only_suppresses_the_private_ip_category(monkeypatch):
    token = "zzqq" + "-pragma-token"
    monkeypatch.setenv("XPST_PUBLIC_SAFETY_DENYLIST", token)
    text = f"host {HOST}  # {PRAGMA} owned by {token}\n"

    findings = scan_text(text, "notes.md")

    # The private address is exempt; the denied identifier on the same line is not.
    assert [finding.kind for finding in findings] == ["user_name"]


# --- Gating behaviour -----------------------------------------------------
def test_tree_scan_passes_and_fails_on_a_private_ip(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(scanner, "ROOT", tmp_path)
    (tmp_path / "ok.py").write_text("VERSION = '1'\n", encoding="utf-8")

    assert scanner.main([]) == 0
    capsys.readouterr()

    (tmp_path / "leak.py").write_text(f'BIND = "{HOST}"\n', encoding="utf-8")

    assert scanner.main([]) == 1
    assert "private_ip" in capsys.readouterr().out


def test_artifact_scan_gates_private_ip(tmp_path):
    archive = tmp_path / "bundle.whl"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("app/config.py", f'BIND = "{HOST}"\n')

    result = scan_artifact(archive)

    assert result["ok"] is False
    assert result["counts_by_category"] == {"private_ip": 1}
    assert HOST not in json.dumps(result)


def test_repository_tree_has_no_unmarked_private_ip():
    # The regression that matters: the checked-in fixtures that deliberately
    # use private addresses carry the pragma, and nothing else leaks one.
    result = scan_public_safety(scanner.ROOT)

    assert result["counts_by_category"].get("private_ip", 0) == 0, result["counts_by_file"]


# --- CI wiring ------------------------------------------------------------
WORKFLOW = scanner.ROOT / ".github" / "workflows" / "public-safety.yml"


def test_ci_workflow_runs_tree_and_artifact_scans_in_gating_mode():
    """The Public Safety job must keep running the scanner as a gate.

    The new category needs no flag: the workflow's tree and artifact steps run
    the scanner without ``--no-fail``, so ``private_ip`` fails the job exactly
    like every other gating category. This test pins that, so a future
    ``--no-fail`` cannot silently turn the IP rule back into review-only.
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")
    scan_steps = [line.strip() for line in workflow.splitlines() if "scan_public_safety.py" in line]
    tree_steps = [line for line in scan_steps if "public-safety-tree.json" in line]
    artifact_steps = [line for line in scan_steps if "public-safety-artifacts.json" in line]

    assert tree_steps, scan_steps
    assert artifact_steps, scan_steps
    for step in tree_steps + artifact_steps:
        assert "--no-fail" not in step, step


# --- History mode stays report-only ---------------------------------------
def _init_repo(path: Path) -> None:
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(path),
        "GIT_AUTHOR_NAME": "Tester",
        "GIT_AUTHOR_EMAIL": "tester@example.com",
        "GIT_COMMITTER_NAME": "Tester",
        "GIT_COMMITTER_EMAIL": "tester@example.com",
    }

    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True, env=env)

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True, capture_output=True)
    run("config", "user.email", "tester@example.com")
    run("config", "user.name", "Tester")
    (path / "readme.md").write_text("hello\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-m", "initial")


def test_history_scan_reports_private_ip_without_gating(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "net.md").write_text(f"bind {HOST}\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True,
                   env={"PATH": "/usr/bin:/bin", "HOME": str(repo)})
    subprocess.run(["git", "commit", "-m", "lan address"], cwd=repo, check=True, capture_output=True,
                   env={"PATH": "/usr/bin:/bin", "HOME": str(repo), "GIT_AUTHOR_NAME": "T",
                        "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "T",
                        "GIT_COMMITTER_EMAIL": "t@example.com"})

    result = scan_history(repo)

    assert "private_ip" in result["counts_by_category_commits"]

    monkeypatch.setattr(scanner, "ROOT", repo)
    assert scanner.main(["--history", "--json"]) == 0
    capsys.readouterr()
