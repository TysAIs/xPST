"""Tests for xPST"""


import pytest


@pytest.fixture
def tmp_dir(tmp_path):
    """Provide a temporary directory for tests"""
    return tmp_path


@pytest.fixture
def sample_config():
    """Provide a sample configuration for tests"""
    from xpst.config import XPSTConfig

    config = XPSTConfig()
    config.tiktok.username = "test_user"
    config.youtube.enabled = True
    config.x.enabled = True
    config.instagram.enabled = True
    return config
