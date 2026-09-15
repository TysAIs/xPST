"""Public repository safety scan tests.

Fixture values are assembled from fragments so the test file itself stays clean
when the scanner (or this same test module) is scanned in CI.
"""

from __future__ import annotations

import json
import subprocess
import tarfile
import zipfile
from typing import TYPE_CHECKING

from scripts import scan_public_safety as scanner
from scripts.scan_public_safety import (
    scan_artifact,
    scan_history,
    scan_public_safety,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

# --- Synthetic fixture fragments (never stored as whole values) -----------
FAKE_USER = "j" + "doe"
MAC_HOME = "/Users/" + FAKE_USER
LINUX_HOME = "/home/" + FAKE_USER
WIN_HOME = "C:" + "\\" + "Users" + "\\" + FAKE_USER
FAKE_EMAIL = "jane.doe" + "@" + "gmail" + "." + "com"
FAKE_PHONE = "+1 415" + "-555" + "-0142"
PRIVATE_KEY_HEADER = "-----BEGIN " + "RSA PRIVATE KEY" + "-----"
HOSTNAME = FAKE_USER + "-macbook-pro" + ".local"
USER_ENV = "USER=" + FAKE_USER + "123"
SERVICE_ACCOUNT_JSON = '{"type":' + ' "service_account", "private_key_id": "abc"}'
GITHUB_TOKEN = "ghp_" + ("A" * 40)
AWS_ACCESS_KEY = "AKIA" + ("A1" * 8)
AWS_SECRET_VALUE = "s" * 40
GOOGLE_API_KEY = "AIza" + ("b" * 35)
GENERIC_TOKEN = "glpat-" + ("c" * 24)


# --- Existing behaviour ---------------------------------------------------
def test_public_safety_scan_flags_sensitive_filename(tmp_path):
    secret_file = tmp_path / "x_cookies.json"
    secret_file.write_text("{}", encoding="utf-8")

    result = scan_public_safety(tmp_path, [secret_file])

    assert result["ok"] is False
    assert result["findings"][0]["kind"] == "sensitive_file"


def test_public_safety_scan_flags_high_confidence_token(tmp_path):
    source = tmp_path / "example.py"
    source.write_text('TOKEN = "' + GITHUB_TOKEN + '"\n', encoding="utf-8")

    result = scan_public_safety(tmp_path, [source])

    assert result["ok"] is False
    assert result["findings"][0]["kind"] == "github_token"


def test_public_safety_scan_allows_documentation_words(tmp_path):
    doc = tmp_path / "README.md"
    doc.write_text("Do not commit tokens, cookies, sessions, or API keys.\n", encoding="utf-8")

    result = scan_public_safety(tmp_path, [doc])

    assert result["ok"] is True
    assert result["findings"] == []


# --- Absolute home paths --------------------------------------------------
def test_flags_macos_home_path(tmp_path):
    source = tmp_path / "build.sh"
    source.write_text('clang -o app "' + MAC_HOME + '/proj/build/app"\n', encoding="utf-8")

    result = scan_public_safety(tmp_path, [source])

    assert [f["kind"] for f in result["findings"]] == ["home_path_macos"]


def test_flags_macos_home_path_in_file_url(tmp_path):
    meta = tmp_path / "direct_url.json"
    meta.write_text('{"url": "file://' + MAC_HOME + '/xPST"}', encoding="utf-8")

    result = scan_public_safety(tmp_path, [meta])

    assert [f["kind"] for f in result["findings"]] == ["home_path_macos"]


def test_flags_linux_home_path(tmp_path):
    source = tmp_path / "deploy.sh"
    source.write_text("cp " + LINUX_HOME + "/app /srv/app\n", encoding="utf-8")

    result = scan_public_safety(tmp_path, [source])

    assert [f["kind"] for f in result["findings"]] == ["home_path_linux"]


def test_flags_windows_home_path(tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("built in " + WIN_HOME + "\\src\\app\n", encoding="utf-8")

    result = scan_public_safety(tmp_path, [source])

    assert [f["kind"] for f in result["findings"]] == ["home_path_windows"]


def test_allows_placeholder_home_paths(tmp_path):
    doc = tmp_path / "INSTALL.md"
    doc.write_text(
        "Use /Users/<user>/xPST, /home/xpst/app, /home/user/app "
        "and C:\\Users\\tester\\AppData\n",
        encoding="utf-8",
    )

    result = scan_public_safety(tmp_path, [doc])

    assert result["ok"] is True, result["findings"]


# --- Personal email -------------------------------------------------------
def test_flags_personal_email(tmp_path):
    doc = tmp_path / "CONTACT.md"
    doc.write_text("Reach the maintainer at " + FAKE_EMAIL + ".\n", encoding="utf-8")

    result = scan_public_safety(tmp_path, [doc])

    assert [f["kind"] for f in result["findings"]] == ["personal_email"]


def test_allows_noreply_and_project_emails(tmp_path):
    doc = tmp_path / "CONTRIBUTING.md"
    doc.write_text(
        "Co-authored-by: dev 12345+dev@" + "users.noreply.github.com\n"
        "Contact: help@" + "example.com\n",
        encoding="utf-8",
    )

    result = scan_public_safety(tmp_path, [doc])

    assert result["ok"] is True, result["findings"]


def test_allows_documentation_placeholder_email(tmp_path):
    doc = tmp_path / "setup-youtube.md"
    doc.write_text("Send it to your" + ".email@" + "gmail.com\n", encoding="utf-8")

    result = scan_public_safety(tmp_path, [doc])

    assert result["ok"] is True, result["findings"]


def test_allows_reserved_test_domain_email(tmp_path):
    doc = tmp_path / "test_ssrf_guard.py"
    payload = "https://user:" + "pw@" + "example.test/x"
    doc.write_text('validate("' + payload + '")\n', encoding="utf-8")

    result = scan_public_safety(tmp_path, [doc])

    assert result["ok"] is True, result["findings"]


def test_allows_synthetic_user_assignment(tmp_path):
    source = tmp_path / "conftest.py"
    source.write_text('USERNAME = "' + "synthetic" + '-admin"\n', encoding="utf-8")

    result = scan_public_safety(tmp_path, [source])

    assert result["ok"] is True, result["findings"]


def test_email_like_asset_path_is_not_flagged(tmp_path):
    conf = tmp_path / "tauri.conf.json"
    conf.write_text('{"icon": "icons/128' + "x128@2x" + '.png"}\n', encoding="utf-8")

    result = scan_public_safety(tmp_path, [conf])

    assert result["ok"] is True, result["findings"]


def test_single_char_email_in_binary_strings_is_not_flagged(tmp_path):
    blob = tmp_path / "font.dat"
    blob.write_bytes(b"\x00\x01" + b"g@" + b"a.co" + b"\x00\x02")

    result = scan_public_safety(tmp_path, [blob])

    assert result["ok"] is True, result["findings"]


# --- Phone numbers --------------------------------------------------------
def test_flags_phone_number(tmp_path):
    doc = tmp_path / "CONTACT.md"
    doc.write_text("Call " + FAKE_PHONE + " for support.\n", encoding="utf-8")

    result = scan_public_safety(tmp_path, [doc])

    assert [f["kind"] for f in result["findings"]] == ["phone_number"]


def test_ignores_dates_versions_and_long_digit_runs(tmp_path):
    doc = tmp_path / "CHANGELOG.md"
    doc.write_text(
        "Released 2026-09-14 build 1234567890123456789 hash 0123456789abcdef0123456789\n",
        encoding="utf-8",
    )

    result = scan_public_safety(tmp_path, [doc])

    assert result["ok"] is True, result["findings"]


# --- Credential shapes ----------------------------------------------------
def test_flags_private_key(tmp_path):
    source = tmp_path / "key.txt"
    source.write_text(PRIVATE_KEY_HEADER + "\nMIIE\n", encoding="utf-8")

    result = scan_public_safety(tmp_path, [source])

    assert [f["kind"] for f in result["findings"]] == ["private_key"]


def test_flags_aws_access_key(tmp_path):
    source = tmp_path / "cfg.py"
    source.write_text('KEY = "' + AWS_ACCESS_KEY + '"\n', encoding="utf-8")

    result = scan_public_safety(tmp_path, [source])

    assert [f["kind"] for f in result["findings"]] == ["aws_access_key"]


def test_flags_aws_secret_key(tmp_path):
    source = tmp_path / "cfg.ini"
    source.write_text("aws_secret_access_key = " + AWS_SECRET_VALUE + "\n", encoding="utf-8")

    result = scan_public_safety(tmp_path, [source])

    assert [f["kind"] for f in result["findings"]] == ["aws_secret_key"]


def test_flags_gcp_service_account(tmp_path):
    source = tmp_path / "sa.json"
    source.write_text(SERVICE_ACCOUNT_JSON + "\n", encoding="utf-8")

    result = scan_public_safety(tmp_path, [source])

    assert [f["kind"] for f in result["findings"]] == ["gcp_service_account"]


def test_flags_google_api_key(tmp_path):
    source = tmp_path / "cfg.py"
    source.write_text('GOOGLE = "' + GOOGLE_API_KEY + '"\n', encoding="utf-8")

    result = scan_public_safety(tmp_path, [source])

    assert [f["kind"] for f in result["findings"]] == ["google_api_key"]


def test_flags_generic_api_token(tmp_path):
    source = tmp_path / "cfg.py"
    source.write_text('TOKEN = "' + GENERIC_TOKEN + '"\n', encoding="utf-8")

    result = scan_public_safety(tmp_path, [source])

    assert [f["kind"] for f in result["findings"]] == ["api_token"]


# --- Machine / user names -------------------------------------------------
def test_flags_hostname_assignment(tmp_path):
    source = tmp_path / "cfg.env"
    source.write_text("hostname=" + HOSTNAME + "\n", encoding="utf-8")

    result = scan_public_safety(tmp_path, [source])

    assert [f["kind"] for f in result["findings"]] == ["machine_name"]


def test_ignores_local_attribute_access(tmp_path):
    source = tmp_path / "code.py"
    source.write_text("value = self.config.local\nother = data.sources.local\n", encoding="utf-8")

    result = scan_public_safety(tmp_path, [source])

    assert result["ok"] is True, result["findings"]


def test_flags_uppercase_user_env_assignment(tmp_path):
    source = tmp_path / "run.sh"
    source.write_text(USER_ENV + "\nexport HOME_PREFIX=/x\n", encoding="utf-8")

    result = scan_public_safety(tmp_path, [source])

    assert [f["kind"] for f in result["findings"]] == ["user_name"]


def test_ignores_lowercase_username_config_field(tmp_path):
    source = tmp_path / "config.py"
    source.write_text("username = self.account.name\nusername: str = 'testuser'\n", encoding="utf-8")

    result = scan_public_safety(tmp_path, [source])

    assert result["ok"] is True, result["findings"]


def test_denylist_env_flags_configured_identifier(tmp_path, monkeypatch):
    monkeypatch.setenv("XPST_PUBLIC_SAFETY_DENYLIST", "zzqq" + "-personal-token")
    source = tmp_path / "notes.md"
    source.write_text("machine zzqq" + "-personal-token is used for builds\n", encoding="utf-8")

    result = scan_public_safety(tmp_path, [source])

    assert [f["kind"] for f in result["findings"]] == ["user_name"]


# --- No matched values in the report --------------------------------------
def test_report_never_contains_matched_values(tmp_path):
    token = "ghp_" + ("Z" * 40)
    source = tmp_path / "leak.py"
    source.write_text('TOKEN = "' + token + '"\n', encoding="utf-8")

    result = scan_public_safety(tmp_path, [source])

    payload = json.dumps(result)
    assert token not in payload
    assert result["ok"] is False
    assert result["counts_by_category"] == {"github_token": 1}
    assert result["counts_by_file"] == {"leak.py": 1}


# --- Artifact scanning ----------------------------------------------------
def test_scan_zip_artifact_finds_leak(tmp_path):
    archive = tmp_path / "bundle.zip"
    payload = "ghp_" + ("B" * 40)
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("app/config.py", 'TOKEN = "' + payload + '"\n')
        zf.writestr("app/README.md", "clean\n")

    result = scan_artifact(archive)

    assert result["ok"] is False
    assert result["counts_by_category"] == {"github_token": 1}
    assert payload not in json.dumps(result)


def test_scan_tar_artifact_finds_home_path(tmp_path):
    inner = tmp_path / "payload.txt"
    inner.write_text("built at " + MAC_HOME + "/proj/dist\n", encoding="utf-8")
    archive = tmp_path / "src.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(inner, arcname="pkg/payload.txt")

    result = scan_artifact(archive)

    assert result["ok"] is False
    assert result["counts_by_category"] == {"home_path_macos": 1}


def test_scan_pyinstaller_onedir_artifact(tmp_path):
    onedir = tmp_path / "xpst-engine"
    onedir.mkdir()
    (onedir / "base_library.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    # A Mach-O/ELF style executable member: build path inside printable strings.
    exe = onedir / "xpst-engine"
    exe.write_bytes(b"\x7fELF" + b"\x00" * 4 + (MAC_HOME + "/src/xpst/engine.py").encode() + b"\x00" * 4)
    (onedir / "README.txt").write_text("clean\n", encoding="utf-8")

    result = scan_artifact(onedir)

    assert result["ok"] is False
    assert result["counts_by_category"] == {"home_path_macos": 1}
    assert any("xpst-engine" in f["path"] for f in result["findings"])


def test_artifact_scan_reports_vendored_email_as_informational(tmp_path):
    archive = tmp_path / "bundle.whl"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(
            "pkg-1.0.dist-info/METADATA",
            "Author: Someone <" + FAKE_EMAIL + ">\n",
        )

    result = scan_artifact(archive)

    assert result["ok"] is True
    assert result["findings"] == []
    assert len(result["informational_findings"]) == 1


def test_artifact_scan_passes_clean_bundle(tmp_path):
    archive = tmp_path / "clean.whl"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("pkg/__init__.py", "VERSION = '1.0'\n")

    result = scan_artifact(archive)

    assert result["ok"] is True
    assert result["findings"] == []


# --- History scanning -----------------------------------------------------
def _init_repo(path: Path) -> None:
    def run(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=path,
            check=True,
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "HOME": str(path),
                "GIT_AUTHOR_NAME": "Tester",
                "GIT_AUTHOR_EMAIL": "tester@example.com",
                "GIT_COMMITTER_NAME": "Tester",
                "GIT_COMMITTER_EMAIL": "tester@example.com",
            },
        )

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True, capture_output=True)
    run("config", "user.email", "tester@example.com")
    run("config", "user.name", "Tester")
    (path / "readme.md").write_text("hello\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-m", "initial")


def test_history_scan_reports_categories_and_commits(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "build.sh").write_text("clang " + MAC_HOME + "/xpst/main.c\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "leak"],
        cwd=repo,
        check=True,
        capture_output=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(repo), "GIT_AUTHOR_NAME": "T",
             "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "T",
             "GIT_COMMITTER_EMAIL": "t@example.com"},
    )
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()

    result = scan_history(repo)

    assert result["mode"] == "history"
    assert result["ok"] is False
    assert "home_path_macos" in result["counts_by_category_commits"]
    assert head in result["category_commits"]["home_path_macos"]
    assert result["raw_values_recorded"] is False
    assert MAC_HOME not in json.dumps(result)


def test_history_scan_clean_repo(tmp_path):
    repo = tmp_path / "clean"
    repo.mkdir()
    _init_repo(repo)

    result = scan_history(repo)

    assert result["ok"] is True
    assert result["categories"] == []
    assert result["commits_scanned"] >= 1


# --- Module import contract ----------------------------------------------
def test_module_exposes_expected_entrypoints():
    assert callable(scanner.scan_public_safety)
    assert callable(scanner.scan_artifact)
    assert callable(scanner.scan_history)
    assert callable(scanner.scan_text)


def test_main_exits_nonzero_on_findings(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(scanner, "ROOT", tmp_path)
    (tmp_path / "leak.py").write_text('T = "' + GITHUB_TOKEN + '"\n', encoding="utf-8")

    code = scanner.main(["--no-fail"])
    out = capsys.readouterr().out
    assert code == 0
    assert "github_token" in out


def test_cli_report_file_written(tmp_path, monkeypatch):
    monkeypatch.setattr(scanner, "ROOT", tmp_path)
    (tmp_path / "ok.md").write_text("clean\n", encoding="utf-8")
    report = tmp_path / "report.json"

    code = scanner.main(["--report", str(report), "--json"])

    assert code == 0
    assert json.loads(report.read_text(encoding="utf-8"))["ok"] is True
