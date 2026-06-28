"""Tests for xPST quota management"""


from xpst.utils.quota import PlatformQuota, QuotaManager


class TestPlatformQuota:
    """Test platform quota tracking"""

    def test_can_upload_within_limits(self):
        """Test that upload is allowed when within limits"""
        quota = PlatformQuota(platform="test", daily_limit=10)

        assert quota.can_upload() is True

    def test_cannot_upload_at_limit(self):
        """Test that upload is blocked at daily limit"""
        quota = PlatformQuota(platform="test", daily_limit=2)
        quota.record_upload()
        quota.record_upload()

        assert quota.can_upload() is False

    def test_remaining_today(self):
        """Test remaining count calculation"""
        quota = PlatformQuota(platform="test", daily_limit=10)
        quota.record_upload()
        quota.record_upload()
        quota.record_upload()

        assert quota.remaining_today() == 7

    def test_hourly_limit(self):
        """Test hourly rate limiting"""
        quota = PlatformQuota(platform="test", daily_limit=100, hourly_limit=5)

        for _ in range(5):
            quota.record_upload()

        assert quota.can_upload() is False
        assert quota.remaining_this_hour() == 0

    def test_to_dict_and_from_dict(self):
        """Test serialization roundtrip"""
        quota = PlatformQuota(platform="test", daily_limit=10)
        quota.record_upload()

        data = quota.to_dict()
        restored = PlatformQuota.from_dict(data)

        assert restored.platform == "test"
        assert restored.daily_limit == 10
        assert restored.used_today == 1


class TestQuotaManager:
    """Test quota management across platforms"""

    def test_create_manager(self, tmp_path):
        """Test creating a quota manager"""
        manager = QuotaManager(str(tmp_path))

        assert "youtube" in manager.quotas
        assert "instagram" in manager.quotas
        assert "x" in manager.quotas

    def test_can_upload_unknown_platform(self, tmp_path):
        """Test that unknown platforms allow uploads"""
        manager = QuotaManager(str(tmp_path))

        assert manager.can_upload("unknown_platform") is True

    def test_record_upload(self, tmp_path):
        """Test recording uploads"""
        manager = QuotaManager(str(tmp_path))

        for _ in range(4):
            manager.record_upload("youtube")

        remaining = manager.get_remaining("youtube")
        assert remaining["daily"] == 1  # Limit is 5, 4 used

    def test_get_status(self, tmp_path):
        """Test getting status for all platforms"""
        manager = QuotaManager(str(tmp_path))

        status = manager.get_status()

        assert "youtube" in status
        assert "instagram" in status
        assert "x" in status

        assert status["youtube"]["daily_limit"] == 5  # Default limit
        assert status["youtube"]["used_today"] == 0
        assert status["youtube"]["remaining"] == 5  # Limit is 5, none used

    def test_persistence(self, tmp_path):
        """Test that quota state persists"""
        manager1 = QuotaManager(str(tmp_path))
        manager1.record_upload("youtube")
        manager1.record_upload("x")
        manager1.save()

        # Load fresh manager
        manager2 = QuotaManager(str(tmp_path))

        assert manager2.quotas["youtube"].used_today == 1
        assert manager2.quotas["x"].used_today == 1
        assert manager2.quotas["instagram"].used_today == 0

    def test_set_x_tier(self, tmp_path):
        """Test changing X tier"""
        manager = QuotaManager(str(tmp_path))

        # Default is conservative free (10)
        assert manager.quotas["x"].daily_limit == 5

        manager.set_x_tier("pro")
        assert manager.quotas["x"].daily_limit == 50000
        assert manager.quotas["x"].hourly_limit == 500

        manager.set_x_tier("free")
        assert manager.quotas["x"].daily_limit == 17
        assert manager.quotas["x"].hourly_limit is None

    def test_quota_exhaustion(self, tmp_path):
        """Test full quota exhaustion"""
        manager = QuotaManager(str(tmp_path))

        # Exhaust YouTube quota (limit is 5)
        for _ in range(5):
            manager.record_upload("youtube")

        assert manager.can_upload("youtube") is False
        assert manager.get_remaining("youtube")["daily"] == 0
