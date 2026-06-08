"""Tests for XPST updater"""


from xpst.updater import (
    PackageInfo,
    _version_is_newer,
    get_installed_version,
    get_xpst_version,
)


class TestVersionChecking:
    """Test version checking functions."""

    def test_get_xpst_version(self):
        """Test that XPST version is returned."""
        ver = get_xpst_version()
        assert ver is not None
        assert ver == "0.1.0"

    def test_get_installed_version_known_package(self):
        """Test getting version of an installed package."""
        # json is always available in stdlib
        ver = get_installed_version("json")
        # json doesn't have __version__, so this may return None
        # That's fine - the function handles missing versions gracefully
        assert ver is None or isinstance(ver, str)

    def test_get_installed_version_missing_package(self):
        """Test getting version of a non-existent package."""
        ver = get_installed_version("nonexistent_package_xyz_123")
        assert ver is None

    def test_version_is_newer_true(self):
        """Test that newer version is detected."""
        assert _version_is_newer("1.0.0", "2.0.0") is True

    def test_version_is_newer_false(self):
        """Test that same version is not newer."""
        assert _version_is_newer("1.0.0", "1.0.0") is False

    def test_version_is_newer_none(self):
        """Test that None versions return False."""
        assert _version_is_newer(None, "1.0.0") is False
        assert _version_is_newer("1.0.0", None) is False


class TestPackageInfo:
    """Test PackageInfo dataclass."""

    def test_package_info_defaults(self):
        """Test default values."""
        info = PackageInfo(name="test")
        assert info.name == "test"
        assert info.current_version is None
        assert info.latest_version is None
        assert info.installed is False
        assert info.updatable is False
        assert info.error is None

    def test_package_info_installed(self):
        """Test installed package info."""
        info = PackageInfo(name="yt-dlp", current_version="2024.1.1", installed=True)
        assert info.installed is True
        assert info.updatable is False

    def test_package_info_updatable(self):
        """Test updatable package info."""
        info = PackageInfo(
            name="yt-dlp",
            current_version="2024.1.1",
            latest_version="2024.2.1",
            installed=True,
            updatable=True,
        )
        assert info.updatable is True
