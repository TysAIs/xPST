from xpst.knowledge.models import Area, Nugget


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


def test_nugget_id_unchanged_when_embedding_or_difficulty_differ():
    base = Nugget.create(point="same point", source_video_id="vid1",
                         timestamp_start=0.0, timestamp_end=1.0)
    embedded = base.with_embedding((0.1, 0.2, 0.3))
    retagged = base.with_difficulty("advanced")
    assert embedded.id == base.id
    assert retagged.id == base.id
    assert embedded.embedding == (0.1, 0.2, 0.3)
    assert retagged.difficulty == "advanced"
    # originals are frozen / unchanged (immutable helpers return new copies)
    assert base.embedding == ()
    assert base.difficulty == "beginner"


def test_nugget_with_area_returns_new_copy_same_id():
    base = Nugget.create(point="p", source_video_id="v",
                         timestamp_start=0.0, timestamp_end=1.0)
    assigned = base.with_area("area-42")
    assert assigned.area_id == "area-42"
    assert assigned.id == base.id
    assert base.area_id is None


def test_nugget_embedding_and_created_at_default():
    n = Nugget.create(point="p", source_video_id="v",
                      timestamp_start=0.0, timestamp_end=1.0)
    assert n.embedding == ()
    assert n.created_at == 0.0


def test_nugget_from_dict_on_old_no_embedding_dict_succeeds():
    # Mirrors a nuggets.json written by Phase 1 (no embedding/created_at keys).
    old = {
        "id": "abc123",
        "point": "old point",
        "source_video_id": "v",
        "timestamp_start": 0.0,
        "timestamp_end": 1.0,
        "source_url": None,
        "area_id": None,
        "difficulty": "beginner",
        "prerequisites": [],
    }
    n = Nugget.from_dict(old)
    assert n.point == "old point"
    assert n.embedding == ()
    assert n.created_at == 0.0


def test_nugget_embedding_roundtrip_is_tuple():
    n = Nugget.create(point="p", source_video_id="v",
                      timestamp_start=0.0, timestamp_end=1.0)
    n = n.with_embedding([0.5, 0.25])  # accept a list, store immutably
    d = n.to_dict()
    assert d["embedding"] == [0.5, 0.25]
    restored = Nugget.from_dict(d)
    assert restored == n
    assert isinstance(restored.embedding, tuple)


def test_area_roundtrip():
    a = Area.create(label="Intro to Topic", centroid=(0.1, 0.2),
                    nugget_ids=("n1", "n2"), order_index=0)
    assert a.id
    restored = Area.from_dict(a.to_dict())
    assert restored == a
    assert isinstance(restored.centroid, tuple)
    assert isinstance(restored.nugget_ids, tuple)


def test_area_create_assigns_stable_id_from_label():
    a = Area.create(label="Networking Basics")
    b = Area.create(label="Networking Basics")
    assert a.id == b.id  # id is a function of label only
    c = Area.create(label="Advanced Routing")
    assert a.id != c.id
