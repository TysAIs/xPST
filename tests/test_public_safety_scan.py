"""Public repository safety scan tests."""

from __future__ import annotations

from scripts.scan_public_safety import scan_public_safety


def test_public_safety_scan_flags_sensitive_filename(tmp_path):
    secret_file = tmp_path / "x_cookies.json"
    secret_file.write_text("{}", encoding="utf-8")

    result = scan_public_safety(tmp_path, [secret_file])

    assert result["ok"] is False
    assert result["findings"][0]["kind"] == "sensitive_file"


def test_public_safety_scan_flags_high_confidence_token(tmp_path):
    source = tmp_path / "example.py"
    source.write_text('TOKEN = "ghp_' + ("A" * 40) + '"\n', encoding="utf-8")

    result = scan_public_safety(tmp_path, [source])

    assert result["ok"] is False
    assert result["findings"][0]["kind"] == "github_token"


def test_public_safety_scan_allows_documentation_words(tmp_path):
    doc = tmp_path / "README.md"
    doc.write_text("Do not commit tokens, cookies, sessions, or API keys.\n", encoding="utf-8")

    result = scan_public_safety(tmp_path, [doc])

    assert result["ok"] is True
    assert result["findings"] == []
