"""The IG retry economy as an executable guard: budget, not a comment.

One throttled Instagram post on the instagrapi (``auth_mode: session``) path —
the mode this box actually runs — used to cost 16 provider requests and ~91-98s
of sleeping, because the uploader looped on a rate limit *and* the service's
``retry_operation`` looped on the uploader. The decision (t_0fabf71f) was one
retry layer, owned by the service: 4 requests / ~13-15s.

This module pins that decision as code and fails loudly if it is broken again:

* the worst case is **derived from the live constants** (``InstagramUploader.
  RETRIES_A_THROTTLE`` / ``MAX_TRANSIENT_RETRIES`` and the service retry config)
  and ``guard()`` raises ``RequestBudgetExceededError`` when a path exceeds the
  decided budget;
* the runtime side counts the provider requests one throttled post really makes
  against that budget, and refuses the request that crosses the recorded ceiling;
* the cooldown state machine is entered exactly ONCE per throttled post — the
  retry layer must not re-strike it on every attempt.

Everything here runs on the failure-injection doubles from
``tests/test_rate_limit_failure_injection.py`` (fake transports, recorded
sleeps). **Zero live posts, zero non-loopback connections** — and this module
proves the socket claim itself instead of inheriting it from a neighbouring run
(loopback is allowed: Windows emulates the event loop's self-pipe with a
127.0.0.1 connect, which says nothing about provider traffic).
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import pytest

from tests.test_rate_limit_failure_injection import (
    THROTTLE_FAULTS,
    Clock,
    InstagramSessionTransport,
    build_uploader,
    make_service,
    make_video,
)
from xpst.utils.request_budget import (
    IG_SESSION_ECONOMY,
    IG_SESSION_RECORDED_CEILING_REQUESTS,
    RequestBudget,
    RequestBudgetExceededError,
    derive_instagram_session_worst_case,
)

#: Loopback is not "the network": on Windows, ``socket.socketpair()`` — which
#: the ProactorEventLoop uses for its self-pipe — is emulated with a real
#: AF_INET connect to 127.0.0.1. Blocking that would fail every test that runs
#: an event loop there, and it would say nothing about provider traffic.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", ""})


def _host_of(address: Any) -> Any:
    return address[0] if isinstance(address, tuple) and address else address


@pytest.fixture(autouse=True)
def socket_guard(monkeypatch):
    """Fail the test if anything in this module opens a NON-loopback connection.

    The provider calls on this path are fake transports, so a real connection
    would mean some other layer reached the network during a dry-run post.
    """
    attempted: list[Any] = []
    real_connect = socket.socket.connect
    real_create_connection = socket.create_connection

    def guarded_connect(self, address):  # noqa: ANN001
        if _host_of(address) in _LOOPBACK_HOSTS:
            return real_connect(self, address)
        attempted.append(address)
        raise AssertionError(
            f"this suite must not call the network, but something connected to {address!r}"
        )

    def guarded_create_connection(address, *a, **kw):  # noqa: ANN001, ANN003
        if _host_of(address) in _LOOPBACK_HOSTS:
            return real_create_connection(address, *a, **kw)
        attempted.append(address)
        raise AssertionError(
            f"this suite must not call the network, but something connected to {address!r}"
        )

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    yield attempted
    assert attempted == [], (
        f"{len(attempted)} outbound socket connection(s) attempted in a dry-run "
        f"suite: {attempted}"
    )


def test_socket_guard_allows_the_loopback_self_pipe_but_not_the_network(socket_guard):
    """The guard's own allow-list is exercised, so it cannot be silently dead.

    Windows emulates ``socket.socketpair()`` with a 127.0.0.1 connect; that must
    pass. A non-loopback address must be refused and recorded.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        client.connect(listener.getsockname())  # loopback: allowed
        assert socket_guard == [], "a loopback connect must not be recorded"
    finally:
        client.close()
        listener.close()

    remote = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(AssertionError, match="must not call the network"):
            remote.connect(("192.0.2.1", 9))  # TEST-NET-1, refused by the guard
    finally:
        remote.close()
    assert socket_guard == [("192.0.2.1", 9)]
    socket_guard.clear()  # this test deliberately attempted one; the gate is 0


@pytest.fixture
def global_sleeps(monkeypatch):
    """Record every ``asyncio.sleep`` (the instagrapi path sleeps internally)."""
    recorded: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float, *a: Any, **kw: Any) -> None:
        recorded.append(float(delay))
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return recorded


# ---------------------------------------------------------------------------
# The decision, derived from the code that implements it
# ---------------------------------------------------------------------------

def test_ig_session_economy_is_derived_from_the_live_retry_constants():
    """The worst case, read off the real uploader and retry config, is 4/one layer.

    This is the guard doing its job on the current tree: if someone re-adds a
    retry layer (``RETRIES_A_THROTTLE = True``), raises ``MAX_TRANSIENT_RETRIES``,
    or bumps the service attempt count, ``guard()`` raises here — in CI — instead
    of a comment going stale.
    """
    worst = derive_instagram_session_worst_case()

    assert worst.uploader_retries_throttle is False, (
        "the uploader retries a throttle again: that re-creates the 4x4 nesting "
        "the retry economy was fixed to remove"
    )
    assert worst.service_attempts == 4
    assert worst.worst_case_requests == 4, (
        f"one throttled post can cost {worst.worst_case_requests} provider "
        f"requests; the decided budget is {IG_SESSION_ECONOMY.requests}"
    )
    assert worst.retry_layers == IG_SESSION_ECONOMY.retry_layers == 1

    # The recorded pre-fix numbers stay quoted in code, so the ceiling a
    # reviewer sees is the measured one (16 requests / ~96s), not a guess.
    assert IG_SESSION_ECONOMY.request_ceiling == IG_SESSION_RECORDED_CEILING_REQUESTS == 16
    assert IG_SESSION_ECONOMY.wait_s <= IG_SESSION_ECONOMY.wait_ceiling_s
    assert IG_SESSION_ECONOMY.wait_ceiling_s == pytest.approx(96.0)

    # And the guard passes on the shipped economy.
    worst.guard()
    print(
        f"MEASURED budget-derived instagram-session: requests={worst.worst_case_requests} "
        f"layers={worst.retry_layers} budget={IG_SESSION_ECONOMY.requests} "
        f"ceiling={IG_SESSION_ECONOMY.request_ceiling}"
    )


def test_economy_guard_fails_loudly_when_the_uploader_retries_a_throttle():
    """A re-introduced nested layer is 16 requests again, and must fail loudly.

    Without this, the previous test could pass on a guard that never fires.
    """
    nested = derive_instagram_session_worst_case(uploader_retries_throttle=True)

    assert nested.retry_layers == 2
    assert nested.worst_case_requests == 16, (
        "nested layering should reproduce the measured 4x4 = 16 requests"
    )

    with pytest.raises(RequestBudgetExceededError) as excinfo:
        nested.guard()

    message = str(excinfo.value)
    assert "2 nested retry layers" in message
    assert "16 requests" in message
    assert "budget of 4" in message
    assert "96" in message, "the failure message must quote the recorded ~96s cost"


def test_request_budget_refuses_the_request_that_crosses_the_recorded_ceiling():
    """The runtime counter fails at request 17, naming the ceiling it crossed."""
    budget = RequestBudget(label="ceiling-probe")
    for _ in range(IG_SESSION_RECORDED_CEILING_REQUESTS):
        budget.spend("clip_upload")

    with pytest.raises(RequestBudgetExceededError) as excinfo:
        budget.spend("clip_upload")

    message = str(excinfo.value)
    assert "provider request 17" in message
    assert "ceiling of 16" in message


def test_request_budget_finish_fails_a_path_that_is_under_the_ceiling_but_over_budget():
    """4 is the budget; 5 is a regression even though 16 is the hard wall."""
    budget = RequestBudget(label="budget-probe")
    for _ in range(IG_SESSION_ECONOMY.requests + 1):
        budget.spend("clip_upload")  # does not raise: still under the ceiling

    with pytest.raises(RequestBudgetExceededError) as excinfo:
        budget.finish(waited_s=1.0)
    assert "5 provider requests" in str(excinfo.value)
    assert "budget 4" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Regression: what ONE throttled post actually costs on the real code path
# ---------------------------------------------------------------------------

class BudgetedSessionTransport(InstagramSessionTransport):
    """The instagrapi double, with every provider request spent from a budget."""

    def __init__(self, fault, budget: RequestBudget) -> None:  # noqa: ANN001
        super().__init__(fault)
        self.budget = budget

    def clip_upload(self, path, caption=None, thumbnail=None):  # noqa: ANN001
        self.budget.spend("clip_upload")
        return super().clip_upload(path, caption, thumbnail)


@pytest.mark.parametrize("fault", THROTTLE_FAULTS, ids=lambda f: f.label)
def test_one_throttled_post_consumes_at_most_the_decided_request_budget(
    fault, tmp_path, monkeypatch, global_sleeps
):
    """4 requests / under the wait budget for one throttled post — measured, not asserted from a docstring."""
    budget = RequestBudget(label=f"throttle/{fault.label}")
    transport = BudgetedSessionTransport(fault, budget)
    uploader, _ = build_uploader(transport, tmp_path, monkeypatch)
    service = make_service(tmp_path, Clock())

    result = asyncio.run(
        post_via_service(service, uploader, tmp_path, video_id=f"vid-budget-{fault.label}")
    )

    assert result.success is False
    assert service.state.is_video_posted(f"vid-budget-{fault.label}", "instagram") is False

    # The measured cost of one throttled post...
    assert budget.requests == transport.calls == IG_SESSION_ECONOMY.requests, (
        f"instagrapi/{fault.label}: one throttled post cost {budget.requests} provider "
        f"requests; the decided budget is {IG_SESSION_ECONOMY.requests} "
        f"(nested layering cost {IG_SESSION_ECONOMY.request_ceiling})"
    )
    # ...must satisfy the same budget the guard checks at runtime.
    budget.finish(waited_s=sum(global_sleeps))

    assert sum(global_sleeps) <= IG_SESSION_ECONOMY.wait_s, (
        f"instagrapi/{fault.label}: slept {sum(global_sleeps):.2f}s for one throttled "
        f"post; the budget is {IG_SESSION_ECONOMY.wait_s}s"
    )
    print(
        f"MEASURED instagram-session/{fault.label}: requests={budget.requests} "
        f"wait_s={sum(global_sleeps):.2f} budget={IG_SESSION_ECONOMY.requests} "
        f"ceiling={IG_SESSION_ECONOMY.request_ceiling}"
    )


@pytest.mark.parametrize("fault", THROTTLE_FAULTS, ids=lambda f: f.label)
def test_cooldown_state_is_entered_once_per_throttled_post(
    fault, tmp_path, monkeypatch, global_sleeps
):
    """One strike per throttled post — not one per retry layer attempt.

    The service's retry layer does retry (that is the decided economy), so this
    asserts both halves: the retries happened, and the cooldown state machine
    was entered exactly once for the whole post. Entering it per attempt would
    escalate the pause ladder 4x as fast on every throttle.
    """
    budget = RequestBudget(label=f"strikes/{fault.label}")
    transport = BudgetedSessionTransport(fault, budget)
    uploader, _ = build_uploader(transport, tmp_path, monkeypatch)
    service = make_service(tmp_path, Clock())

    hits: list[str] = []
    real_record_hit = service.rate_cooldowns.record_hit

    def spy_record_hit(platform: str, **kwargs: Any):  # noqa: ANN003
        hits.append(platform)
        return real_record_hit(platform, **kwargs)

    service.rate_cooldowns.record_hit = spy_record_hit

    result = asyncio.run(
        post_via_service(service, uploader, tmp_path, video_id=f"vid-strike-{fault.label}")
    )

    assert result.success is False
    # The property under test: ONE entry into the cooldown state machine for the
    # whole post — the retry layer must not re-strike it on every attempt.
    assert hits == ["instagram"], (
        f"cooldown state entered {len(hits)} time(s) ({hits}) for one throttled "
        f"post; it must be entered once per post, not once per retry layer"
    )
    record = service.rate_cooldowns.snapshot()["instagram"]
    assert record["strikes"] == 1, (
        f"one throttled post recorded {record['strikes']} strikes — the ladder "
        f"escalates once per post, not once per retry layer"
    )
    assert service.rate_cooldowns.remaining_s("instagram") > 0
    # ...and that measurement is only meaningful over the decided attempt count:
    # the service retry layer DOES retry (4 attempts), the uploader does not.
    assert transport.calls == IG_SESSION_ECONOMY.requests, (
        f"expected the one service retry layer to make "
        f"{IG_SESSION_ECONOMY.requests} attempts, got {transport.calls} provider "
        f"requests — a nested uploader loop is back"
    )
    budget.finish(waited_s=sum(global_sleeps))


async def post_via_service(service, uploader, tmp_path, video_id: str):
    """Post one video through the real service path (same call the engine makes)."""
    return await service.upload_to_platform(
        uploader=uploader,
        video_path=make_video(tmp_path),
        caption="retry economy",
        platform_name="instagram",
        video_id=video_id,
    )
