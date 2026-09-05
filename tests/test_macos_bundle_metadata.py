"""Regression checks for macOS bundle metadata."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_bundle_version_comes_from_project():
    spec = (ROOT / "build_macos.spec").read_text()
    assert '"CFBundleVersion": project_version' in spec
    assert '"CFBundleShortVersionString": project_version' in spec


def test_bundle_preserves_installed_identity():
    spec = (ROOT / "build_macos.spec").read_text()
    assert 'bundle_identifier="com.tysais.xpst"' in spec


def test_bundle_has_source_provenance():
    spec = (ROOT / "build_macos.spec").read_text()
    assert '"XPSTSourceCommit": source_commit' in spec
    assert re.search(r'git.*rev-parse.*HEAD', spec)
