"""The public updater channel refuses what it used to publish silently.

Three defects motivated this file, all in `.github/workflows/publish-updater.yml`:

* the manifest's version came from the tag's spelling (`${tag#v}`), so a
  `v1.3.1` tag cut on a 1.3.0 tree would advertise 1.3.1 while the assets were
  named `xPST_1.3.0_*` — every client offered an update that reinstalls the bits
  it already has, forever;
* nothing compared the candidate with the manifest already served on `main`, so
  re-publishing an older release moved the channel backwards and no client was
  offered an update again, silently;
* neither publishing path verified a signature against `plugins.updater.pubkey`,
  so a rotation that left the config on the old key produced a channel no client
  could install from.

These tests exercise `scripts/verify-updater-channel.py` on real git trees and
really signed bytes; the workflow wiring is asserted in
`tests/test_publish_updater_workflow.py`.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_updater_channel", ROOT / "scripts/verify-updater-channel.py"
)
assert SPEC and SPEC.loader
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)

VERSION_SURFACES = (
    "pyproject.toml",
    "Cargo.toml",
    "src/xpst/__init__.py",
    "src-tauri/Cargo.toml",
    "src-tauri/tauri.conf.json",
    "ui/package.json",
    "ui/package-lock.json",
)


# --------------------------------------------------------------------------
# fixtures: real Ed25519 keys, real minisign-shaped signatures, real git trees
# --------------------------------------------------------------------------
class Signer:
    """A minisign-shaped keypair whose signatures this repository can verify."""

    def __init__(self, key_id: bytes = bytes.fromhex("0102030405060708")) -> None:
        self.private = Ed25519PrivateKey.generate()
        self.key_id = key_id
        self.public_bytes = self.private.public_key().public_bytes_raw()

    @property
    def pubkey(self) -> str:
        inner = b"Ed" + self.key_id + self.public_bytes
        text = (
            "untrusted comment: minisign public key: 0102030405060708\n"
            f"{base64.b64encode(inner).decode()}\n"
        )
        return base64.b64encode(text.encode()).decode()

    def sign(self, payload: bytes, *, prehashed: bool = True, algorithm: bytes = b"ED") -> str:
        message = hashlib.blake2b(payload, digest_size=64).digest() if prehashed else payload
        inner = algorithm + self.key_id + self.private.sign(message)
        return (
            "untrusted comment: signature from tauri secret key\n"
            f"{base64.b64encode(inner).decode()}\n"
            "trusted comment: timestamp:1794783965\tfile:xPST\n"
            f"{base64.b64encode(bytes(range(64))).decode()}\n"
        )


@pytest.fixture()
def signer() -> Signer:
    return Signer()


def _write_tree(root: Path, version: str, *, pubkey: str, overrides: dict[str, str] | None = None) -> None:
    overrides = overrides or {}
    files = {
        "pyproject.toml": f'[project]\nname = "xpst"\nversion = "{version}"\n',
        "Cargo.toml": f'[workspace]\nmembers = []\n\n[workspace.package]\nversion = "{version}"\n',
        "src/xpst/__init__.py": f'__version__ = "{version}"\n',
        "src-tauri/Cargo.toml": f'[package]\nname = "xpst-shell"\nversion = "{version}"\n',
        "src-tauri/tauri.conf.json": json.dumps(
            {"productName": "xPST", "version": version, "plugins": {"updater": {"pubkey": pubkey}}}
        ),
        "ui/package.json": json.dumps({"name": "xpst-ui", "version": version}),
        "ui/package-lock.json": json.dumps({"version": version, "packages": {"": {"version": version}}}),
    }
    files.update(overrides)
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@example.invalid")
    return repo


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "--all")
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture()
def release_repo(tmp_path: Path, signer: Signer) -> Path:
    """A repo whose tag `v1.3.0` names a tree that declares 1.3.0."""
    repo = _init_repo(tmp_path)
    _write_tree(repo, "1.3.0", pubkey=signer.pubkey)
    _commit(repo, "release 1.3.0")
    _git(repo, "tag", "v1.3.0")
    return repo


# --------------------------------------------------------------------------
# version provenance
# --------------------------------------------------------------------------
def test_release_surfaces_constant_covers_everything_verify_release_version_reads(tmp_path: Path) -> None:
    """A surface added to `verify_release_version` must be added here too.

    The tag's tree is materialised from exactly `guard.RELEASE_SURFACES`;
    an eighth surface would be silently left behind, and the guard would compare
    half a tree.
    """
    assert set(guard.RELEASE_SURFACES) == set(VERSION_SURFACES)
    for relative in VERSION_SURFACES:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((ROOT / relative).read_text(encoding="utf-8"), encoding="utf-8")

    from scripts.verify_release_version import release_versions

    assert release_versions(tmp_path) == release_versions(ROOT)


def test_version_comes_from_the_tag_tree(release_repo: Path) -> None:
    assert guard.resolve_channel_version(release_repo, "v1.3.0") == "1.3.0"


def test_version_is_read_from_the_tag_not_from_the_checkout(tmp_path: Path, signer: Signer) -> None:
    """The publishing job checks out `main`; the tag's tree is the right one.

    This is the case the old `${tag#v}` avoided by accident: the checkout is
    always the served branch, so the version must be fetched from the tag.
    """
    repo = _init_repo(tmp_path)
    _write_tree(repo, "1.2.1", pubkey=signer.pubkey)
    _commit(repo, "release 1.2.1")
    _git(repo, "tag", "v1.2.1")
    _write_tree(repo, "1.3.0", pubkey=signer.pubkey)
    _commit(repo, "prepare 1.3.0")
    _git(repo, "tag", "v1.3.0")

    assert guard.resolve_channel_version(repo, "v1.2.1") == "1.2.1"
    assert guard.resolve_channel_version(repo, "v1.3.0") == "1.3.0"


def test_a_mistagged_release_is_refused(tmp_path: Path, signer: Signer) -> None:
    """`v1.3.1` on a 1.3.0 tree must fail instead of advertising 1.3.1."""
    repo = _init_repo(tmp_path)
    _write_tree(repo, "1.3.0", pubkey=signer.pubkey)
    _commit(repo, "release 1.3.0")
    _git(repo, "tag", "v1.3.1")

    with pytest.raises(guard.ChannelError) as excinfo:
        guard.resolve_channel_version(repo, "v1.3.1")
    message = str(excinfo.value)
    assert "v1.3.1" in message
    assert "1.3.0" in message


def test_disagreeing_release_surfaces_are_refused(tmp_path: Path, signer: Signer) -> None:
    repo = _init_repo(tmp_path)
    _write_tree(repo, "1.3.0", pubkey=signer.pubkey, overrides={"ui/package.json": json.dumps({"version": "1.2.1"})})
    _commit(repo, "half-bumped tree")
    _git(repo, "tag", "v1.3.0")

    with pytest.raises(guard.ChannelError) as excinfo:
        guard.resolve_channel_version(repo, "v1.3.0")
    assert "disagree" in str(excinfo.value)


@pytest.mark.parametrize("ref", ["dryrun-tauri-7", "main", "release/1.3.0", "v1.3", "1.3.0"])
def test_a_non_release_ref_is_refused(release_repo: Path, ref: str) -> None:
    with pytest.raises(guard.ChannelError) as excinfo:
        guard.resolve_channel_version(release_repo, ref)
    assert "not a vX.Y.Z release tag" in str(excinfo.value)


def test_the_cli_prints_the_version_and_fails_a_mistag(release_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert guard.main(["version", "--repo", str(release_repo), "--tag", "v1.3.0"]) == 0
    assert capsys.readouterr().out.strip() == "1.3.0"

    _git(release_repo, "tag", "v1.3.1")
    assert guard.main(["version", "--repo", str(release_repo), "--tag", "v1.3.1"]) == 2
    assert "refused" in capsys.readouterr().err


# --------------------------------------------------------------------------
# monotonicity
# --------------------------------------------------------------------------
def test_versions_order_the_way_the_updater_orders_them() -> None:
    assert guard.semver_key("1.10.0") > guard.semver_key("1.9.0")
    assert guard.semver_key("2.0.0") > guard.semver_key("2.0.0-rc.1")
    assert guard.semver_key("1.3.0-rc.10") > guard.semver_key("1.3.0-rc.2")
    assert guard.semver_key("1.3.0") == guard.semver_key("1.3.0")


def test_equal_and_higher_versions_are_allowed() -> None:
    assert "first" in guard.check_monotonic(None, "1.3.0")
    assert "upgrades from" in guard.check_monotonic("1.2.1", "1.3.0")
    assert "re-publishes" in guard.check_monotonic("1.3.0", "1.3.0")


def test_a_downgrade_is_refused_unless_explicitly_allowed() -> None:
    with pytest.raises(guard.ChannelError) as excinfo:
        guard.check_monotonic("1.3.0", "1.2.1")
    assert "lower than" in str(excinfo.value)
    assert "--allow-downgrade" in str(excinfo.value)

    assert "allowed" in guard.check_monotonic("1.3.0", "1.2.1", allow_downgrade=True)


def test_the_served_manifest_is_read_from_the_git_ref(release_repo: Path) -> None:
    assert guard.read_served_manifest(release_repo, "HEAD:updates/latest.json") is None

    target = release_repo / "updates/latest.json"
    target.parent.mkdir()
    target.write_text(json.dumps({"version": "1.3.0", "platforms": {"darwin-aarch64": {}}}), encoding="utf-8")
    _commit(release_repo, "publish the channel")

    served = guard.read_served_manifest(release_repo, "HEAD:updates/latest.json")
    assert served is not None
    assert served["version"] == "1.3.0"


# --------------------------------------------------------------------------
# asset-name cross-check
# --------------------------------------------------------------------------
def test_asset_names_must_agree_with_the_resolved_version() -> None:
    names = [
        "xPST_1.3.0_aarch64.dmg",
        "xPST_1.3.0_x64-setup.exe",
        "xPST_1.3.0_amd64.AppImage",
        "darwin-aarch64-xPST.app.tar.gz",
        "SHA256SUMS",
    ]
    assert "3 versioned asset name(s)" in guard.check_asset_names(names, "1.3.0")

    with pytest.raises(guard.ChannelError) as excinfo:
        guard.check_asset_names(names, "1.3.1")
    message = str(excinfo.value)
    assert "xPST_1.3.0_aarch64.dmg" in message
    assert "1.3.1" in message


def test_asset_names_without_a_version_are_not_guessed_at() -> None:
    assert "0 versioned asset name(s)" in guard.check_asset_names(["xPST.app.tar.gz", "NOTES.md"], "1.3.0")


# --------------------------------------------------------------------------
# signatures
# --------------------------------------------------------------------------
def _payload(tmp_path: Path, name: str = "xPST.app.tar.gz", content: bytes = b"payload bytes\n") -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


def test_a_real_signature_verifies(tmp_path: Path, signer: Signer) -> None:
    guard.verify_payload(_payload(tmp_path), signer.sign(b"payload bytes\n"), signer.pubkey)


def test_the_signature_is_over_the_blake2b_prehash_not_the_raw_bytes(tmp_path: Path, signer: Signer) -> None:
    """Tauri signs through rsign2, which prehashes with Blake2b-512.

    Verifying the raw bytes is the mistake this rule prevents: it fails for
    every genuine Tauri signature.
    """
    payload = _payload(tmp_path)
    raw = signer.sign(payload.read_bytes(), prehashed=False)
    with pytest.raises(guard.ChannelError) as excinfo:
        guard.verify_payload(payload, raw, signer.pubkey)
    assert "Blake2b-512" in str(excinfo.value)


def test_a_tampered_payload_is_refused(tmp_path: Path, signer: Signer) -> None:
    signature = signer.sign(b"payload bytes\n")
    tampered = _payload(tmp_path, content=b"payload bytez\n")
    with pytest.raises(guard.ChannelError):
        guard.verify_payload(tampered, signature, signer.pubkey)


def test_a_signature_from_another_key_is_refused(tmp_path: Path, signer: Signer) -> None:
    """A rotation that left the config on the old pubkey must not publish."""
    other = Signer(key_id=bytes.fromhex("1112131415161718"))
    with pytest.raises(guard.ChannelError) as excinfo:
        guard.verify_payload(_payload(tmp_path), other.sign(b"payload bytes\n"), signer.pubkey)
    assert "no client could install" in str(excinfo.value)


def test_the_legacy_unprehashed_algorithm_is_accepted(tmp_path: Path, signer: Signer) -> None:
    """`Ed` is a real minisign algorithm; it signs the raw message."""
    guard.verify_payload(
        _payload(tmp_path), signer.sign(b"payload bytes\n", prehashed=False, algorithm=b"Ed"), signer.pubkey
    )


def test_malformed_signatures_are_refused(tmp_path: Path, signer: Signer) -> None:
    payload = _payload(tmp_path)
    with pytest.raises(guard.ChannelError):
        guard.verify_payload(payload, "not-a-signature", signer.pubkey)
    with pytest.raises(guard.ChannelError):
        guard.verify_payload(payload, base64.b64encode(b"too short").decode(), signer.pubkey)


def test_the_manifest_signature_shape_is_accepted(tmp_path: Path, signer: Signer) -> None:
    """`latest.json` carries base64-of-the-sig-text, not the text itself."""
    payload = _payload(tmp_path)
    text = signer.sign(payload.read_bytes())
    assert guard.verify_payload(payload, text, signer.pubkey) is None
    assert guard.verify_payload(payload, base64.b64encode(text.encode()).decode(), signer.pubkey) is None


# --------------------------------------------------------------------------
# the whole gate
# --------------------------------------------------------------------------
def _manifest(tmp_path: Path, signer: Signer, version: str = "1.3.0") -> tuple[dict, Path]:
    assets = tmp_path / "assets"
    assets.mkdir()
    entries = {}
    for platform, name, content in (
        ("darwin-aarch64", "darwin-aarch64-xPST.app.tar.gz", b"arm64 payload\n"),
        ("windows-x86_64", "windows-x86_64-xPST-setup.exe", b"windows payload\n"),
    ):
        path = assets / name
        path.write_bytes(content)
        entries[platform] = {
            "signature": base64.b64encode(signer.sign(content).encode()).decode(),
            "sha512": hashlib.sha512(content).hexdigest(),
            "url": f"https://github.com/TysAIs/xPST/releases/download/v{version}/{name}",
        }
    return {"version": version, "notes": "release notes", "platforms": entries}, assets


def test_the_gate_verifies_every_advertised_payload(tmp_path: Path, signer: Signer) -> None:
    manifest, assets = _manifest(tmp_path, signer)
    assert guard.verify_manifest(manifest, assets, signer.pubkey, "1.3.0") == ["darwin-aarch64", "windows-x86_64"]


def test_the_gate_refuses_a_manifest_advertising_another_version(tmp_path: Path, signer: Signer) -> None:
    manifest, assets = _manifest(tmp_path, signer, version="1.3.1")
    with pytest.raises(guard.ChannelError) as excinfo:
        guard.verify_manifest(manifest, assets, signer.pubkey, "1.3.0")
    assert "1.3.1" in str(excinfo.value)


def test_the_gate_refuses_bytes_that_do_not_match_the_manifest(tmp_path: Path, signer: Signer) -> None:
    manifest, assets = _manifest(tmp_path, signer)
    (assets / "darwin-aarch64-xPST.app.tar.gz").write_bytes(b"swapped payload\n")
    with pytest.raises(guard.ChannelError) as excinfo:
        guard.verify_manifest(manifest, assets, signer.pubkey, "1.3.0")
    assert "sha512" in str(excinfo.value)


def test_the_gate_refuses_a_payload_missing_from_the_release(tmp_path: Path, signer: Signer) -> None:
    manifest, assets = _manifest(tmp_path, signer)
    (assets / "windows-x86_64-xPST-setup.exe").unlink()
    with pytest.raises(guard.ChannelError) as excinfo:
        guard.verify_manifest(manifest, assets, signer.pubkey, "1.3.0")
    assert "not among the release assets" in str(excinfo.value)


def test_the_gate_refuses_a_manifest_with_no_platforms(tmp_path: Path, signer: Signer) -> None:
    with pytest.raises(guard.ChannelError):
        guard.verify_manifest({"version": "1.3.0", "platforms": {}}, tmp_path, signer.pubkey, "1.3.0")


def test_the_verify_command_accepts_a_good_publish_and_refuses_a_downgrade(
    release_repo: Path, signer: Signer, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest, assets = _manifest(tmp_path, signer)
    manifest_path = tmp_path / "latest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    names = tmp_path / "names.txt"
    names.write_text("xPST_1.3.0_aarch64.dmg\nxPST_1.3.0_x64-setup.exe\n", encoding="utf-8")

    argv = [
        "verify",
        "--repo",
        str(release_repo),
        "--tag",
        "v1.3.0",
        "--manifest",
        str(manifest_path),
        "--assets-dir",
        str(assets),
        "--asset-names-file",
        str(names),
    ]
    assert guard.main(argv) == 0
    assert capsys.readouterr().out.strip() == "1.3.0"

    downgrade = [*argv, "--served-version", "1.3.1"]
    assert guard.main(downgrade) == 2
    assert "lower than" in capsys.readouterr().err

    forced = [*downgrade, "--allow-downgrade"]
    assert guard.main(forced) == 0
    capsys.readouterr()
