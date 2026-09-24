// Pure draft logic for the compose screen: what a draft holds, what to restore
// when the screen is opened again, and how to word a stale plan.
//
// Deliberately free of DOM/Svelte state so it can be unit tested with
// `node --test` and reused by any screen. The rule that matters most: a stale
// plan is never presented as ready, and a refusal always says why.

/** Fields a draft owns. Anything else the engine keeps is read-only to us. */
export function emptyDraft() {
  return { draftId: "", mediaPath: "", caption: "", platforms: {}, verdict: null, restored: false };
}

/** Version tag the engine writes into its draft file (informational). */
export const DRAFT_STORE_VERSION = 1;

/** True when there is anything worth persisting. */
export function hasDraftContent({ mediaPath = "", caption = "", platforms = {} } = {}) {
  const chosen = Object.values(platforms ?? {}).filter(Boolean);
  return Boolean(String(mediaPath ?? "").trim() || String(caption ?? "") || chosen.length);
}

/** The destination names currently selected in the compose checkboxes. */
export function selectedPlatforms(platforms = {}) {
  return Object.entries(platforms ?? {})
    .filter(([, checked]) => Boolean(checked))
    .map(([name]) => name);
}

/** Request body for `POST /api/drafts` (autosave and preflight both use it). */
export function draftRequest({ draftId = "", mediaPath = "", caption = "", platforms = {} } = {}) {
  const body = {
    media_paths: String(mediaPath ?? "").trim() ? [String(mediaPath).trim()] : [],
    caption: String(caption ?? ""),
    platforms: selectedPlatforms(platforms),
  };
  if (draftId) body.draft_id = draftId;
  return body;
}

/**
 * Newest in-progress draft from a `/api/drafts` payload, or null.
 *
 * Posted drafts are never restored: reopening the app must not resurrect a post
 * that already went out.
 */
export function newestDraft(payload) {
  const rows = Array.isArray(payload?.drafts) ? payload.drafts : [];
  for (const row of rows) {
    const draft = row?.draft;
    if (!draft || !draft.id) continue;
    if (String(draft.status ?? "") === "posted") continue;
    return row;
  }
  return null;
}

/** Compose-screen state restored from a stored draft row. */
export function restoreDraft(row) {
  if (!row?.draft) return emptyDraft();
  const draft = row.draft;
  const paths = Array.isArray(draft.media_paths) ? draft.media_paths : [];
  const platforms = {};
  for (const name of Array.isArray(draft.platforms) ? draft.platforms : []) {
    platforms[String(name)] = true;
  }
  return {
    draftId: String(draft.id ?? ""),
    mediaPath: String(paths[0] ?? ""),
    caption: String(draft.caption ?? ""),
    platforms,
    verdict: row.verdict ?? null,
    restored: true,
  };
}

/** Human label for what the engine knows about a draft's plan. */
export function draftStatusLabel(verdict) {
  if (!verdict) return "Draft";
  if (verdict.stale) return "Plan out of date";
  if (verdict.planned) return verdict.plan_ready ? "Plan is current" : "Plan blocked";
  return "Draft";
}

/** Badge tone for a draft verdict (matches StatusBadge's vocabulary). */
export function draftStatusTone(verdict) {
  if (!verdict) return "unknown";
  if (verdict.stale) return "warning";
  if (verdict.planned) return verdict.plan_ready ? "success" : "error";
  return "disabled";
}

/**
 * What the screen should say about the current plan.
 *
 * `requiresReconfirmation` is true only for a *plan* that went stale — that is
 * the state the engine refuses to post without an explicit re-confirmation.
 */
export function planBanner(verdict) {
  if (!verdict) {
    return { show: false, tone: "unknown", title: "", detail: "", reasons: [], requiresReconfirmation: false };
  }
  const reasons = Array.isArray(verdict.reasons) ? verdict.reasons : [];
  if (verdict.stale) {
    return {
      show: true,
      tone: "warning",
      title: "This plan is out of date",
      detail: reasons.length
        ? "The engine will refuse to post this until you check again or re-confirm."
        : "The engine will refuse to post this until you re-confirm.",
      reasons,
      requiresReconfirmation: Boolean(verdict.planned),
    };
  }
  if (verdict.planned) {
    return {
      show: true,
      tone: verdict.plan_ready ? "success" : "error",
      title: verdict.plan_ready ? "Checked and current" : "Checked: the engine would block this",
      detail: verdict.plan_ready
        ? `Validated ${verdict.validated_at ?? "just now"}. Nothing that would change the outcome has moved since.`
        : (verdict.plan_blockers ?? []).join("; "),
      reasons: [],
      requiresReconfirmation: false,
    };
  }
  return { show: false, tone: "unknown", title: "", detail: "", reasons: [], requiresReconfirmation: false };
}

/**
 * Whether an auto-save is needed, and whether the plan was invalidated.
 *
 * Saving is skipped when nothing changed, so typing then navigating away does
 * not write on every keystroke-driven re-render.
 */
export function autosaveDecision(previous, next) {
  if (!hasDraftContent(next)) return { save: false, reset: Boolean(previous?.draftId), reason: "empty" };
  const key = (value) => JSON.stringify(draftRequest(value));
  if (previous?.draftId && key(previous) === key(next)) return { save: false, reset: false, reason: "unchanged" };
  return { save: true, reset: false, reason: "changed" };
}

/** True when a 409 body from `/api/post` was a stale-plan refusal. */
export function isStaleRefusal(body) {
  return Boolean(body && typeof body === "object" && body.stale === true);
}

/** Wording for the refusal, listing the engine's own reasons verbatim. */
export function refusalText(body) {
  const reasons = Array.isArray(body?.stale_reasons) ? body.stale_reasons : [];
  if (body?.unknown_draft) {
    return "This draft is no longer stored on disk, so the post was refused. Save the draft again and retry.";
  }
  if (reasons.length) {
    return `Refused: ${reasons.map((reason) => reason.message).join(" ")}`;
  }
  const blockers = Array.isArray(body?.blockers) ? body.blockers : [];
  return blockers.length ? `Refused: ${blockers.join(" ")}` : "The engine refused this post.";
}