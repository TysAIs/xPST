"""Tests for xPST updater"""


from xpst.updater import (
    PackageInfo,
    UpdateComponent,
    _annotate_component,
    _version_is_newer,
    check_provider_metadata,
    check_update_components,
    get_installed_version,
    get_xpst_version,
)


class TestVersionChecking:
    """Test version checking functions."""

    def test_get_xpst_version(self):
        """Test that xPST version is returned."""
        ver = get_xpst_version()
        assert ver is not None
        assert ver == "0.1.0rc2"

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


class TestUpdateComponents:
    """Test structured update status for release readiness."""

    def test_update_component_to_dict(self):
        component = UpdateComponent(
            name="FFmpeg",
            component_type="helper",
            current_version="6.1",
            installed=True,
            update_mode="external",
            required=True,
        )

        data = component.to_dict()

        assert data["name"] == "FFmpeg"
        assert data["component_type"] == "helper"
        assert data["installed"] is True
        assert data["update_mode"] == "external"
        assert data["status"] == "unknown"
        assert "action" in data
        assert "update_command" in data

    def test_missing_pip_component_gets_safe_update_action(self):
        component = _annotate_component(
            UpdateComponent(
                name="yt-dlp",
                component_type="package",
                installed=False,
                update_mode="pip",
                required=True,
            )
        )

        data = component.to_dict()

        assert data["status"] == "missing"
        assert data["action"] == "Install yt-dlp with xPST's updater."
        assert data["update_command"] == "xpst update"

    def test_update_available_component_gets_update_command(self):
        component = _annotate_component(
            UpdateComponent(
                name="instagrapi",
                component_type="package",
                current_version="1.0.0",
                latest_version="1.1.0",
                installed=True,
                update_mode="pip",
                updatable=True,
            )
        )

        data = component.to_dict()

        assert data["status"] == "update_available"
        assert data["action"] == "Update instagrapi with xPST's updater."
        assert data["update_command"] == "xpst update"

    def test_provider_metadata_is_explicit(self):
        metadata = check_provider_metadata()

        assert metadata[0].name == "provider-manifests"
        assert metadata[0].component_type == "provider_metadata"
        assert metadata[0].update_mode == "bundled"
        assert metadata[0].status == "current"
        assert metadata[0].action == "Provider metadata is bundled; update xPST to refresh it."

    def test_check_update_components_offline_shape(self):
        status = check_update_components(include_network=False)

        assert set(status) == {"app", "packages", "helpers", "provider_metadata"}
        assert status["app"][0]["name"] == "xpst"
        assert status["app"][0]["status"] == "unknown"
        assert status["app"][0]["action"]
        assert any(item["name"] == "yt-dlp" for item in status["helpers"])
        assert any(item["name"] == "FFmpeg" for item in status["helpers"])
        assert all("action" in item for section in status.values() for item in section)
