"""Focused tests for the Tauri updater manifest generator."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("gen_updater_manifest", ROOT / "scripts/gen-updater-manifest.py")
assert SPEC and SPEC.loader
manifest_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manifest_module)

PLATFORMS = (
    "darwin-aarch64",
    "darwin-x86_64",
    "windows-x86_64",
    "linux-x86_64",
)
FILENAMES = {
    "darwin-aarch64": "xPST.app.tar.gz",
    "darwin-x86_64": "xPST-intel.app.tar.gz",
    "windows-x86_64": "xPST-setup.exe",
    "linux-x86_64": "xPST.AppImage",
}
SHA512 = {
    "darwin-aarch64": "3786f5c89dd59448209665a080bebc4446b9c57c7f5067fa9b690ece8e7c0b82c8149d77522b91f943b82823d3ae7de3bf766f7316de029dc436a17bc1c83758",
    "darwin-x86_64": "377658e536ad89be70b0225791527527ab39c41e6d70073b0e38426bdd7a4ec81ca89a73edae9296393669eb249a04c49a96db8273aa42778e71a868bb11a970",
    "windows-x86_64": "321b4c02d8d36878672cb66c84d7c4f307a81f640135ee8923abdf8c23c0a10b2b626482c61f9f267cd4c453585d37e8325dadccd1cb3f355aa44e573bc04903",
    "linux-x86_64": "2741e2c52588a897a3ad5e8942648bc86df09eb8b548799eaf11ae998a6332f49ddbd6e05b0a878f2d6de851d3ff3fcc37c00b4c4f2426ad96134156bca69748",
}


def _fixtures(tmp_path: Path) -> dict[str, Path]:
    artifacts: dict[str, Path] = {}
    for platform in PLATFORMS:
        artifact = tmp_path / FILENAMES[platform]
        artifact.write_bytes(f"{platform} fixture\n".encode())
        artifact.with_name(artifact.name + ".sig").write_text(
            f"untrusted comment: fixture signature for {platform}\n"
            f"signature-{platform}\n",
            encoding="utf-8",
        )
        artifacts[platform] = artifact
    return artifacts


def test_generate_manifest_is_deterministic_and_contains_sha512(tmp_path: Path) -> None:
    artifacts = _fixtures(tmp_path)

    manifest = manifest_module.generate_manifest(
        version="1.2.3",
        notes="Updater fixture release",
        artifacts=artifacts,
        base_url="https://downloads.example.test/v1.2.3",
        pubkey="fixture-public-key",
    )

    assert manifest == {
        "version": "1.2.3",
        "notes": "Updater fixture release",
        "platforms": {
            platform: {
                "signature": f"untrusted comment: fixture signature for {platform}\nsignature-{platform}",
                "sha512": SHA512[platform],
                "url": f"https://downloads.example.test/v1.2.3/{FILENAMES[platform]}",
            }
            for platform in PLATFORMS
        },
    }


def test_generate_manifest_fails_when_signature_is_missing(tmp_path: Path) -> None:
    artifacts = _fixtures(tmp_path)
    (artifacts["linux-x86_64"].with_name("xPST.AppImage.sig")).unlink()

    with pytest.raises(FileNotFoundError, match="signature for linux-x86_64"):
        manifest_module.generate_manifest(
            version="1.2.3",
            notes="Updater fixture release",
            artifacts=artifacts,
            base_url="https://downloads.example.test/v1.2.3",
            pubkey="fixture-public-key",
        )
