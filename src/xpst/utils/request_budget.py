"""Per-post provider-request budget: the retry economy, written as code.

A destination's retry economy is how many provider requests — and how many
seconds of sleeping — ONE logical (throttled) post is allowed to cost before it
gives up. That is a product invariant, not a test detail: nested retry layers
multiplied it once, and the regression is easy to re-introduce by "just adding
a retry" in either layer.

The measured numbers this module pins come from the failure-injection suite
(``tests/test_rate_limit_failure_injection.py``, ``MEASURED`` lines, fake
transports, no socket):

    Instagram instagrapi (``instagram.auth_mode: session``) — the mode this box
    actually runs:

        nested retry layers (uploader loop x service loop)  16 requests / ~91-98 s
        one retry layer (the decided economy)                4 requests / ~13-15 s

``16 requests / ~96 s`` is the recorded ceiling: the worst case the nested
layering produced, and the hard wall a single throttled post must never cross
again. ``4 requests / ~15 s`` is the decided budget after the fix — one retry
layer, owned by the service's ``retry_operation``; the uploader hands a throttle
straight back.

How the guard is meant to be used: derive the worst case from the code's real
constants (uploader retry policy, service attempt count) and call ``guard()``.
It raises ``RequestBudgetExceededError`` loudly when a path exceeds the budget, so a
re-introduced retry layer fails a test instead of silently spending 4x the
requests against a provider that just asked for less traffic.

The guard changes nothing on the posting path by itself: it is a policy object
plus a fail-loud check, exercised by the suite (test doubles only, zero network
posts).
"""

from __future__ import annotations

from dataclasses import dataclass

#: The measured nested-layering cost that must never come back.
IG_SESSION_RECORDED_CEILING_REQUESTS = 16
IG_SESSION_RECORDED_CEILING_WAIT_S = 96.0


class RequestBudgetExceededError(RuntimeError):
    """A code path spent more provider requests than its retry economy allows."""


@dataclass(frozen=True)
class RetryEconomy:
    """What one logical post on a destination is allowed to cost.

    ``requests``/``wait_s`` are the decided budget (a path above them is a
    regression). ``request_ceiling``/``wait_ceiling_s`` are the recorded
    pre-fix numbers: the hard wall nothing may cross, and the context quoted in
    the failure message.
    """

    platform: str
    requests: int
    request_ceiling: int
    wait_s: float
    wait_ceiling_s: float
    retry_layers: int

    def describe(self) -> str:
        return (
            f"{self.platform}: {self.requests} requests / {self.wait_s:.1f}s per "
            f"throttled post, {self.retry_layers} retry layer(s) "
            f"(hard ceiling {self.request_ceiling} requests / "
            f"{self.wait_ceiling_s:.0f}s)"
        )


#: The decided retry economy for the Instagram instagrapi path. One retry
#: layer; the service owns it. See the module docstring for the measurements.
IG_SESSION_ECONOMY = RetryEconomy(
    platform="instagram-session",
    requests=4,
    request_ceiling=IG_SESSION_RECORDED_CEILING_REQUESTS,
    wait_s=19.5,
    wait_ceiling_s=IG_SESSION_RECORDED_CEILING_WAIT_S,
    retry_layers=1,
)


@dataclass(frozen=True)
class DerivedWorstCase:
    """The worst case a concrete code path can reach, from its real constants."""

    economy: RetryEconomy
    uploader_transient_retries: int
    uploader_retries_throttle: bool
    service_attempts: int

    @property
    def retry_layers(self) -> int:
        """1 when a throttle stops at the service layer, 2 when it nests."""
        return 2 if self.uploader_retries_throttle else 1

    @property
    def worst_case_requests(self) -> int:
        """Provider requests one throttled post can cost on this path."""
        if self.uploader_retries_throttle:
            # The uploader loops on the throttle, and the service loops on the
            # uploader: the layers multiply.
            return (self.uploader_transient_retries + 1) * self.service_attempts
        return self.service_attempts

    def violations(self) -> list[str]:
        """Every way this path breaks the decided economy (empty when pinned)."""
        found: list[str] = []
        if self.retry_layers > self.economy.retry_layers:
            found.append(
                f"{self.retry_layers} nested retry layers — the economy allows "
                f"{self.economy.retry_layers}"
            )
        if self.worst_case_requests > self.economy.request_ceiling:
            found.append(
                f"{self.worst_case_requests} requests exceeds the recorded ceiling "
                f"of {self.economy.request_ceiling} "
                f"(~{self.economy.wait_ceiling_s:.0f}s of sleeping per post)"
            )
        elif self.worst_case_requests > self.economy.requests:
            found.append(
                f"{self.worst_case_requests} requests exceeds the decided budget of "
                f"{self.economy.requests} for {self.economy.platform} "
                f"(nested layering cost {self.economy.request_ceiling} requests / "
                f"~{self.economy.wait_ceiling_s:.0f}s)"
            )
        return found

    def guard(self) -> DerivedWorstCase:
        """Raise ``RequestBudgetExceededError`` if this path breaks the economy."""
        problems = self.violations()
        if problems:
            raise RequestBudgetExceededError(
                f"retry economy violated for {self.economy.platform}: "
                + "; ".join(problems)
                + f". Allowed: {self.economy.describe()}."
            )
        return self


def derive_instagram_session_worst_case(
    *,
    economy: RetryEconomy = IG_SESSION_ECONOMY,
    uploader_transient_retries: int | None = None,
    uploader_retries_throttle: bool | None = None,
    service_attempts: int | None = None,
) -> DerivedWorstCase:
    """Derive the Instagram session worst case from the code's real constants.

    Defaults are read from the live uploader class and from the service's retry
    configuration, so a change that re-introduces a nested retry layer changes
    what this returns — and ``guard()`` then fails loudly instead of a comment
    being quietly wrong.
    """
    if (
        uploader_transient_retries is None
        or uploader_retries_throttle is None
        or service_attempts is None
    ):
        from xpst.platforms.instagram import InstagramUploader
        from xpst.utils.retry import STANDARD_RETRY

        if uploader_transient_retries is None:
            uploader_transient_retries = InstagramUploader.MAX_TRANSIENT_RETRIES
        if uploader_retries_throttle is None:
            uploader_retries_throttle = InstagramUploader.RETRIES_A_THROTTLE
        if service_attempts is None:
            service_attempts = STANDARD_RETRY.max_retries + 1

    return DerivedWorstCase(
        economy=economy,
        uploader_transient_retries=uploader_transient_retries,
        uploader_retries_throttle=uploader_retries_throttle,
        service_attempts=service_attempts,
    )


class RequestBudget:
    """Counts the provider requests one logical post actually spends.

    ``spend()`` raises as soon as a request would cross the recorded ceiling, so
    a runaway path fails loudly at the request that broke it instead of
    finishing quietly. ``finish()`` checks the total against the decided budget
    — a path can be under the hard ceiling and still be a regression, and that
    is what the failure message says.
    """

    def __init__(self, economy: RetryEconomy = IG_SESSION_ECONOMY, label: str = "post") -> None:
        self.economy = economy
        self.label = label
        self.requests = 0
        self.waited_s = 0.0
        self.log: list[str] = []

    def spend(self, what: str = "request") -> int:
        """Record one provider request; raise at the recorded ceiling."""
        self.requests += 1
        self.log.append(what)
        if self.requests > self.economy.request_ceiling:
            raise RequestBudgetExceededError(
                f"{self.economy.platform}/{self.label}: provider request "
                f"{self.requests} crosses the recorded ceiling of "
                f"{self.economy.request_ceiling} requests / "
                f"~{self.economy.wait_ceiling_s:.0f}s per throttled post. "
                f"Last requests: {self.log[-4:]}"
            )
        return self.requests

    def finish(self, waited_s: float = 0.0) -> None:
        """Check the finished post against the decided budget; raise if over."""
        self.waited_s = waited_s
        problems: list[str] = []
        if self.requests > self.economy.requests:
            problems.append(
                f"{self.requests} provider requests (decided budget "
                f"{self.economy.requests}; nested layering cost "
                f"{self.economy.request_ceiling})"
            )
        if waited_s > self.economy.wait_s:
            problems.append(
                f"{waited_s:.2f}s of sleeping (decided budget "
                f"{self.economy.wait_s:.1f}s; nested layering cost "
                f"{self.economy.wait_ceiling_s:.0f}s)"
            )
        if problems:
            raise RequestBudgetExceededError(
                f"retry economy violated for {self.economy.platform}/{self.label}: "
                + "; ".join(problems)
            )


__all__ = [
    "DerivedWorstCase",
    "IG_SESSION_ECONOMY",
    "IG_SESSION_RECORDED_CEILING_REQUESTS",
    "IG_SESSION_RECORDED_CEILING_WAIT_S",
    "RequestBudget",
    "RequestBudgetExceededError",
    "RetryEconomy",
]
