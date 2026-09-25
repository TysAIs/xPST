"""One aggregate verdict for a batch of posts, shared by every surface.

``xpst post`` answers "did this post publish?" with an exit code
(:func:`post_exit_code`). Commands that post a *batch* — ``xpst run``,
``xpst backfill``, ``xpst schedule run`` — ask the same question about several
results at once. That aggregate was computed on the CLI and nowhere else, so
the same batch could be a non-zero exit on the command line and an unqualified
success document over MCP — the false-success class the CLI fix removed,
still reachable by an agent.

The rule lives here, once, and every surface reads it:

* **Posting rule (``xpst post``).** A post where nothing was published never
  exits ``0``. When every attempted destination failed, the code names the
  shared reason: ``4`` quota / rate limit, ``3`` authentication, ``10`` no
  destination was attempted at all or every destination is unavailable /
  refused the media, ``1`` for anything else, including a mix of reasons. A
  partial success exits ``0`` (something was published).
* **Batch rule (``xpst run`` / ``xpst backfill`` / ``xpst schedule run``).** The
  same families, aggregated per result: ``0`` when at least one result
  published something, or when there was nothing to do at all ("no new videos"
  / "nothing due" — nothing was attempted because there was nothing to do);
  otherwise the shared family of the results that failed.

:func:`batch_outcome` returns that verdict as data, so a surface without an
exit status (MCP, HTTP) can report exactly what the CLI would exit with, plus
the per-destination failure summary.

Documented in ``docs/TUTORIAL_CLI.md`` → "Exit Codes Reference".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from xpst.engine import CrossPostResult

# ── Meaningful exit codes ───────────────────────
# Note: Click itself uses exit code 2 for usage errors (bad command/options),
# so CONFIG_ERROR is aligned with 2 and 3/4 carry auth/rate-limit.
EXIT_SUCCESS = 0
EXIT_GENERAL = 1
EXIT_CONFIG_ERROR = 2
EXIT_AUTH_FAILURE = 3
EXIT_RATE_LIMIT = 4
EXIT_PLATFORM_UNAVAILABLE = 10

# Failure families, matched against the error text an uploader returns
# ("CODE: message") or its plain-language variant. Order matters: quota is
# checked before auth, because "quota exceeded" never means re-authenticate.
POST_RATE_MARKERS = (
    "QUOTA_EXHAUSTED",
    "RATE_LIMITED",
    "RATE_LIMIT",
    "TOO MANY REQUESTS",
    "429",
)
POST_AUTH_MARKERS = (
    "AUTH_EXPIRED",
    "AUTH_FAILURE",
    "AUTHENTICATION",
    "SESSION_EXPIRED",
    "INVALID_GRANT",
    "NOT_CONFIGURED",
    "UNAUTHORIZED",
    "TOKEN EXPIRED",
    "LOGIN REQUIRED",
    "CREDENTIALS",
    "401",
)
POST_UNAVAILABLE_MARKERS = (
    "NEEDS_URL",
    "UNSUPPORTED",
    "NOT SUPPORTED",
    "UNAVAILABLE",
    "NOT AVAILABLE",
    "DISABLED",
)

POST_EXIT_CODE_LABELS = {
    EXIT_GENERAL: "post failed",
    EXIT_AUTH_FAILURE: "authentication failed",
    EXIT_RATE_LIMIT: "quota or rate limit reached",
    EXIT_PLATFORM_UNAVAILABLE: "destination unavailable",
}

NO_ATTEMPTED_DESTINATION_REASON = "no destination was attempted"

# Batch verdicts (``batch_outcome()["status"]``).
BATCH_PUBLISHED = "published"
BATCH_PARTIAL = "partial"
BATCH_FAILED = "failed"
BATCH_NOTHING_TO_DO = "nothing_to_do"


def post_failure_exit_code(error: str | None) -> int:
    """Map one destination's failure text to the exit-code family it belongs to."""

    text = (error or "").upper()
    for markers, code in (
        (POST_RATE_MARKERS, EXIT_RATE_LIMIT),
        (POST_AUTH_MARKERS, EXIT_AUTH_FAILURE),
        (POST_UNAVAILABLE_MARKERS, EXIT_PLATFORM_UNAVAILABLE),
    ):
        if any(marker in text for marker in markers):
            return code
    return EXIT_GENERAL


def post_failure_message(exit_code: int) -> str:
    """One-line human summary of why a post published nothing."""

    return (
        f"nothing was published — {POST_EXIT_CODE_LABELS.get(exit_code, 'post failed')} "
        f"(exit code {exit_code})."
    )


def attempted(rows: dict[str, Any]) -> dict[str, Any]:
    """The rows an uploader actually ran for.

    A row carrying ``already_posted`` was an idempotent no-op, not an attempt:
    it neither published nor failed, so it must not decide the verdict.
    """

    return {
        platform: upload
        for platform, upload in rows.items()
        if "already_posted" not in (upload.metadata or {})
    }


def post_exit_code(result: CrossPostResult, requested: list[str] | None) -> int:
    """Exit code for one ``xpst post`` run — the single rule above.

    Args:
        result: the CrossPostResult the engine returned.
        requested: the platform names the caller asked for, or None for
            "all enabled platforms" (the engine then decides).

    Returns:
        The exit code the CLI should terminate with.
    """

    rows = result.results
    if not rows:
        # Nothing was attempted (no destination was available/enabled), so
        # nothing was posted: a failure, not a silent success.
        return EXIT_PLATFORM_UNAVAILABLE

    attempted_rows = attempted(rows)
    if not attempted_rows:
        # Every destination was already posted — nothing to do, nothing failed.
        return EXIT_SUCCESS
    if any(upload.success for upload in attempted_rows.values()):
        # A partial success is a success; the per-platform detail is in the report.
        return EXIT_SUCCESS

    codes = {post_failure_exit_code(upload.error) for upload in attempted_rows.values()}

    # A requested destination with no result row was never attempted at all
    # (its uploader is not available) — that failed the caller too.
    requested_norm = {name.strip().lower() for name in (requested or []) if name.strip()}
    if requested_norm - {name.strip().lower() for name in rows}:
        codes.add(EXIT_PLATFORM_UNAVAILABLE)

    return codes.pop() if len(codes) == 1 else EXIT_GENERAL


def aggregate_batch_exit_code(results: list[CrossPostResult]) -> int:
    """Exit code for a command that posts several results in one run.

    ``run`` and ``backfill`` post a *batch*: one invocation can process several
    videos across several destinations. The rule is the one ``xpst post``
    already uses (:func:`post_exit_code`), applied per result and then
    aggregated:

    * no results at all → ``0``: nothing was attempted because there was
      nothing to do ("no new videos") — that is not a failure;
    * any result that published something (or found it already posted) → ``0``;
    * otherwise the shared failure family of the failed results: ``4`` quota /
      rate limit, ``3`` authentication, ``10`` no destination attempted or
      every destination unavailable / refusing the media, ``1`` for anything
      else, including a mix of reasons.

    Args:
        results: the ``CrossPostResult`` list the engine returned, in order.

    Returns:
        The exit code the CLI should terminate with.
    """

    return batch_outcome(results)["exit_code"]


def batch_outcome(results: list[CrossPostResult]) -> dict[str, Any]:
    """The aggregate verdict of a batch, as data (see :func:`aggregate_batch_exit_code`).

    Returns:
        A dict with:

        * ``status``: ``published`` (everything attempted published),
          ``partial`` (something published *and* something failed — the exit
          code stays ``0`` because a partial success is a success, but the
          failure is named), ``failed`` (nothing was published), or
          ``nothing_to_do`` (nothing was attempted because there was nothing
          to do: no results, or every destination already posted);
        * ``exit_code``: the code the CLI would terminate with;
        * counts: ``results`` (results in the batch), ``attempted`` /
          ``published`` / ``failed`` (destinations);
        * ``failed_destinations``: one entry per failed destination —
          ``video_id``, ``platform``, ``attempted``, ``error``, ``exit_code``,
          ``reason``. A result whose destinations were never attemptable is
          reported once with ``platform: None`` and
          ``reason: "no destination was attempted"``, so a batch that fails
          with no per-platform row still names what went wrong.
    """

    failed_destinations: list[dict[str, Any]] = []
    attempted_count = 0
    published_count = 0
    outcomes: list[int] = []

    for result in results:
        rows = attempted(result.results)
        attempted_count += len(rows)
        published_count += sum(1 for upload in rows.values() if upload.success)
        for platform, upload in rows.items():
            if upload.success:
                continue
            code = post_failure_exit_code(upload.error)
            failed_destinations.append(
                {
                    "video_id": result.video_id,
                    "platform": platform,
                    "attempted": True,
                    "error": upload.error,
                    "exit_code": code,
                    "reason": POST_EXIT_CODE_LABELS.get(code, "post failed"),
                }
            )
        code = post_exit_code(result, None)
        outcomes.append(code)
        if code != EXIT_SUCCESS and not rows:
            # No destination row at all: nothing could be attempted for this
            # video, which is its own failure (exit 10) and deserves a name.
            failed_destinations.append(
                {
                    "video_id": result.video_id,
                    "platform": None,
                    "attempted": False,
                    "error": None,
                    "exit_code": code,
                    "reason": NO_ATTEMPTED_DESTINATION_REASON,
                }
            )

    if not outcomes or any(code == EXIT_SUCCESS for code in outcomes):
        # Nothing to do at all, or at least one result published something
        # (a partial success — or an idempotent "already posted" — is a success).
        exit_code = EXIT_SUCCESS
    else:
        failed_codes = {code for code in outcomes if code != EXIT_SUCCESS}
        exit_code = failed_codes.pop() if len(failed_codes) == 1 else EXIT_GENERAL

    if not results or (exit_code == EXIT_SUCCESS and not attempted_count):
        status = BATCH_NOTHING_TO_DO
    elif exit_code != EXIT_SUCCESS:
        status = BATCH_FAILED
    elif failed_destinations:
        status = BATCH_PARTIAL
    else:
        status = BATCH_PUBLISHED

    return {
        "status": status,
        "exit_code": exit_code,
        "results": len(results),
        "attempted": attempted_count,
        "published": published_count,
        "failed": len(failed_destinations),
        "failed_destinations": failed_destinations,
    }
