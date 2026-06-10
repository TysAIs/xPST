from xpst.knowledge.models import Nugget


def test_nugget_id_is_stable_hash_of_source_and_point():
    a = Nugget.create(point="hello world", source_video_id="vid1",
                      timestamp_start=0.0, timestamp_end=5.0)
    b = Nugget.create(point="hello world", source_video_id="vid1",
                      timestamp_start=99.0, timestamp_end=100.0)
    assert a.id == b.id  # id depends only on source_video_id + point


def test_nugget_id_changes_with_point():
    a = Nugget.create(point="one", source_video_id="vid1",
                      timestamp_start=0.0, timestamp_end=1.0)
    b = Nugget.create(point="two", source_video_id="vid1",
                      timestamp_start=0.0, timestamp_end=1.0)
    assert a.id != b.id


def test_nugget_roundtrip():
    n = Nugget.create(point="p", source_video_id="v", source_url="http://x",
                      timestamp_start=1.0, timestamp_end=2.0)
    assert Nugget.from_dict(n.to_dict()) == n
