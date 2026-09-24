// Pure first-run flow logic: wizard steps, destination rows, and the truthful
// wording for a post result.
//
// Everything here is deliberately free of DOM/Svelte state so it can be unit
// tested with `node --test` and reused by the screens. The rule that matters
// most: no helper ever reports success for an operation that did not happen.

/** Routes that make up the first-run flow, in order. */
export const FLOW_ROUTES = [
  { id: "onboarding", href: "#/onboarding", label: "1 · Set up xPST" },
  { id: "connect", href: "#/connect", label: "2 · Connect a platform" },
  { id: "compose", href: "#/compose", label: "3 · Compose a post" },
  { id: "result", href: "#/result", label: "4 · See the result" },
];

const DESTINATION_STATE_LABELS = {
  ready: "Ready to publish",
  unconfigured: "Not connected",
  degraded: "Needs attention",
  blocked_external_review: "Blocked by provider review",
  disabled: "Disabled",
};

/** Human wording for a canonical destination state. Never implies success. */
export function destinationStateLabel(state) {
  const key = String(state ?? "").trim().toLowerCase();
  return DESTINATION_STATE_LABELS[key] ?? (key ? key.replaceAll("_", " ") : "Unknown");
}

/** Badge status for a canonical destination state. */
export function destinationStateStatus(state) {
  const key = String(state ?? "").trim().toLowerCase();
  if (key === "ready") return "success";
  if (key === "disabled") return "disabled";
  if (key === "blocked_external_review" || key === "degraded") return "warning";
  if (key === "unconfigured") return "invalid";
  return "unknown";
}

/**
 * Normalize the destination list from either payload shape the engine sends:
 * `/api/providers` (`{providers: [...]}`) or `/api/onboarding`
 * (`{destinations: [...]}`).
 */
export function destinationRows(payload) {
  const list = payload?.providers ?? payload?.destinations ?? [];
  return list
    .filter((provider) => (provider?.roles ?? ["video_destination"]).includes("video_destination"))
    .map((provider) => {
      const role = provider?.role_status?.video_destination ?? {};
      const state = role.state ?? provider.destination_state ?? "unconfigured";
      // A source-only platform (TikTok before its Content Posting API app is
      // approved) has no posting destination at all. Its role state describes
      // the uploader, so rendering it as "Not connected" would read as
      // "connect it and post" — an upload the engine cannot deliver.
      const sourceOnly = Boolean(provider.source_only ?? role.source_only);
      return {
        name: provider.name,
        displayName: provider.display_name ?? provider.name,
        enabled: Boolean(provider.enabled ?? role.enabled),
        state,
        stateLabel: sourceOnly
          ? "Source only — not a posting destination"
          : destinationStateLabel(state),
        status: sourceOnly ? "source_only" : destinationStateStatus(state),
        ready: !sourceOnly && state === "ready",
        canPost: sourceOnly ? false : Boolean(provider.can_post ?? state === "ready"),
        sourceOnly,
        note: provider.posting_note ?? null,
        authMode: provider.auth_mode ?? role.auth_mode ?? "unknown",
        official: Boolean(provider.is_official_api ?? provider.official_api),
        error: role.error ?? provider.destination_error ?? null,
      };
    });
}

/** Enabled destinations that the engine reports ready for publishing. */
export function readyDestinations(payload) {
  return destinationRows(payload).filter((row) => row.ready);
}

/** Wizard step rows for the onboarding screen, with the next step marked. */
export function stepRows(payload) {
  const rows = payload?.steps ?? [];
  const firstPending = rows.findIndex((step) => !step.done);
  return rows.map((step, index) => ({
    id: step.id,
    title: step.title,
    done: Boolean(step.done),
    current: index === firstPending,
  }));
}

/**
 * Whether the shell should open the onboarding wizard for this load.
 *
 * Only a fresh install (first_run_complete false) landing on the default
 * route is redirected — an explicit navigation to another section is never
 * hijacked, and an unknown/failed payload never redirects.
 */
export function shouldStartOnboarding(payload, hash) {
  const landing = String(hash ?? "") === "" || String(hash ?? "") === "#" || String(hash ?? "") === "#/";
  if (!landing) return false;
  if (!payload || typeof payload !== "object") return false;
  return payload.first_run_complete === false;
}

/** Subset of a post payload that is safe to keep in the session store. */
export function postRequestSummary(request) {
  if (!request) return null;
  return {
    media_paths: [...(request.media_paths ?? [])],
    caption: String(request.caption ?? ""),
    overrides: { ...(request.overrides ?? {}) },
    captions: { ...(request.captions ?? {}) },
    platforms: [...(request.platforms ?? [])],
    dry_run: Boolean(request.dry_run),
  };
}

/**
 * Classify a post envelope. Returns one of:
 *   "idle" | "dry-run-ready" | "dry-run-blocked" | "success" | "partial" |
 *   "failed" | "blocked"
 *
 * "success" requires a real (non dry-run) attempt where at least one
 * destination published and none failed.
 */
export function resultTone(envelope) {
  if (!envelope || typeof envelope !== "object") return "idle";
  const uploaded = Number(envelope.uploaded_count ?? 0);
  const failed = Number(envelope.failed_count ?? 0);
  const blockers = envelope.blockers ?? [];
  if (envelope.dry_run) {
    return envelope.ok && !blockers.length ? "dry-run-ready" : "dry-run-blocked";
  }
  if (blockers.length && uploaded === 0) return "blocked";
  if (uploaded > 0 && failed > 0) return "partial";
  if (uploaded > 0 && failed === 0) return "success";
  return "failed";
}

/** Badge status token for a tone. */
export function toneStatus(tone) {
  switch (tone) {
    case "success":
      return "success";
    case "partial":
      return "warning";
    case "dry-run-ready":
      return "ok";
    case "failed":
    case "blocked":
    case "dry-run-blocked":
      return "error";
    default:
      return "unknown";
  }
}

/** Headline copy for a post envelope. Never claims an upload that did not run. */
export function resultHeadline(envelope) {
  const tone = resultTone(envelope);
  const uploaded = Number(envelope?.uploaded_count ?? 0);
  const failed = Number(envelope?.failed_count ?? 0);
  switch (tone) {
    case "dry-run-ready":
      return "Dry run — checked, nothing was uploaded";
    case "dry-run-blocked":
      return "Dry run — this post would be refused";
    case "success":
      return `Published to ${uploaded} destination${uploaded === 1 ? "" : "s"}`;
    case "partial":
      return `Published to ${uploaded} of ${uploaded + failed} destinations`;
    case "blocked":
      return "Not uploaded — blocked before posting";
    case "failed":
      return "Nothing was uploaded";
    default:
      return "No post has run in this session";
  }
}

/** One-line explanation under the headline. */
export function resultDescription(envelope) {
  const tone = resultTone(envelope);
  if (tone === "idle") return "Compose a post, then this screen shows exactly what the engine did.";
  if (tone === "dry-run-ready") {
    return "The engine verified the media, caption, targets and quota without contacting a platform.";
  }
  if (tone === "dry-run-blocked") {
    const blockers = envelope?.blockers ?? [];
    return blockers.length ? `Blocked by: ${blockers.join("; ")}` : "The engine refused this post.";
  }
  if (tone === "blocked") return `Blocked by: ${(envelope?.blockers ?? []).join("; ")}`;
  if (tone === "success") return "Every requested destination confirmed a published post.";
  if (tone === "partial") {
    return "Some destinations failed. The failures below are the exact errors the engine returned.";
  }
  return "Every requested destination failed. No post was published.";
}

/**
 * Per-destination outcome row: `success: null` means "not attempted" (a dry
 * run), never "fine".
 */
export function destinationOutcomes(envelope) {
  const rows = envelope?.destinations ?? [];
  return rows.map((row) => {
    const attempted = row.attempted ?? row.success !== null;
    let label;
    let status;
    if (!attempted) {
      label = "Not attempted (dry run)";
      status = "disabled";
    } else if (row.success) {
      label = "Published";
      status = "success";
    } else {
      label = "Failed";
      status = "error";
    }
    return {
      platform: row.platform,
      attempted,
      success: row.success === true,
      label,
      status,
      detail: row.error ?? (row.post_url || ""),
      postUrl: row.post_url ?? null,
      retryable: row.retryable ?? null,
      caption: row.caption ?? null,
    };
  });
}

/** Compact "n of m" summary used by the compose screen before posting. */
export function targetSummary(rows) {
  const list = rows ?? [];
  const ready = list.filter((row) => row.ready);
  return {
    total: list.length,
    ready: ready.length,
    names: ready.map((row) => row.displayName),
    blocked: list.filter((row) => !row.ready).map((row) => row.displayName),
  };
}

/**
 * Whether the compose screen may start a post, and why not when it may not.
 *
 * The shipped defect this guards: the post button was enabled with a video and
 * zero destinations selected, so it started a run that could not publish
 * anything (and, before the engine reported results honestly, could read as a
 * success). The reason string is the same wording the CLI, the MCP server and
 * the HTTP API refuse with, so the UI explains the rule instead of restating it
 * differently.
 */
export const NO_DESTINATIONS_REASON = "Choose at least one destination platform.";

export function composePostState({ mediaPath = "", chosen = [], busy = false } = {}) {
  const destinations = chosen ?? [];
  if (busy) return { canPost: false, reason: "", count: destinations.length };
  if (!mediaPath) return { canPost: false, reason: "Choose a video before posting.", count: destinations.length };
  if (!destinations.length) {
    return { canPost: false, reason: NO_DESTINATIONS_REASON, count: 0 };
  }
  return { canPost: true, reason: "", count: destinations.length };
}

/** Label for the post button; never claims a destination count it does not have. */
export function composePostLabel({ dryRun = false, count = 0 } = {}) {
  if (count === 0) return dryRun ? "Run dry run" : "Post";
  const noun = `destination${count === 1 ? "" : "s"}`;
  return dryRun ? `Run dry run for ${count} ${noun}` : `Post to ${count} ${noun}`;
}

/** Human file size for the media picker. */
export function formatBytes(bytes) {
  const value = Number(bytes ?? 0);
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let index = 0;
  let size = value;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size >= 10 || index === 0 ? Math.round(size) : size.toFixed(1)} ${units[index]}`;
}