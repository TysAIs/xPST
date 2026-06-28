from xpst.knowledge.models import Nugget
from xpst.knowledge.store.json_store import JsonKnowledgeStore


def _nugget(point="p", vid="v"):
    return Nugget.create(point=point, source_video_id=vid,
                         timestamp_start=0.0, timestamp_end=1.0)


def test_add_and_get(tmp_path):
    store = JsonKnowledgeStore(tmp_path / "nuggets.json")
    n = _nugget()
    store.add_nugget(n)
    assert store.get_nugget(n.id) == n


def test_has_nugget_and_idempotency(tmp_path):
    store = JsonKnowledgeStore(tmp_path / "nuggets.json")
    n = _nugget()
    store.add_nugget(n)
    store.add_nugget(n)  # second add is a no-op
    assert store.has_nugget(n.id)
    assert len(list(store.all_nuggets())) == 1


def test_persists_across_instances(tmp_path):
    path = tmp_path / "nuggets.json"
    JsonKnowledgeStore(path).add_nugget(_nugget())
    reloaded = JsonKnowledgeStore(path)
    assert len(list(reloaded.all_nuggets())) == 1
