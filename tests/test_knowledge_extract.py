import pytest

from xpst.knowledge.ingest.extract import ExtractionError, extract_nuggets
from xpst.knowledge.ingest.transcribe import Segment, Transcript


def _transcript():
    return Transcript(text="Routers forward packets. Switches connect hosts.",
                      segments=[
                          Segment(start=0.0, end=2.0, text="Routers forward packets."),
                          Segment(start=2.0, end=4.0, text="Switches connect hosts."),
                      ])


class _CannedClient:
    """Returns a sequence of canned chat_json payloads; raises if exhausted."""

    def __init__(self, *payloads):
        self._payloads = list(payloads)
        self.calls = 0

    def chat_json(self, messages):
        self.calls += 1
        if not self._payloads:
            raise AssertionError("chat_json called more than expected")
        return self._payloads.pop(0)


def test_extract_returns_nugget_dicts():
    client = _CannedClient({
        "nuggets": [
            {"point": "Routers forward packets between networks.",
             "timestamp_start": 0.0, "timestamp_end": 2.0},
            {"point": "Switches connect hosts on a LAN.",
             "timestamp_start": 2.0, "timestamp_end": 4.0},
        ]
    })
    out = extract_nuggets(_transcript(), client)
    assert len(out) == 2
    assert out[0]["point"].startswith("Routers")
    assert out[0]["timestamp_start"] == 0.0
    assert client.calls == 1


def test_extract_clamps_timestamps_to_transcript_bounds():
    client = _CannedClient({
        "nuggets": [
            {"point": "Out of range clip.",
             "timestamp_start": -5.0, "timestamp_end": 999.0},
        ]
    })
    out = extract_nuggets(_transcript(), client)
    assert out[0]["timestamp_start"] == 0.0
    assert out[0]["timestamp_end"] == 4.0  # transcript ends at 4.0


def test_extract_retries_once_then_succeeds():
    # First payload is malformed (missing 'point'); a single repair retry fixes it.
    client = _CannedClient(
        {"nuggets": [{"timestamp_start": 0.0, "timestamp_end": 1.0}]},
        {"nuggets": [{"point": "Recovered point.",
                      "timestamp_start": 0.0, "timestamp_end": 1.0}]},
    )
    out = extract_nuggets(_transcript(), client)
    assert out[0]["point"] == "Recovered point."
    assert client.calls == 2


def test_extract_raises_after_repair_fails():
    client = _CannedClient(
        {"nuggets": [{"bad": "shape"}]},
        {"nuggets": "still wrong"},
    )
    with pytest.raises(ExtractionError):
        extract_nuggets(_transcript(), client)
    assert client.calls == 2


def test_extract_empty_nuggets_is_allowed():
    client = _CannedClient({"nuggets": []})
    out = extract_nuggets(_transcript(), client)
    assert out == []
