// Thin fetch client over the xPST engine /api endpoints plus the hash-router
// helpers the App shell needs. Same-origin relative paths: in dev, Vite
// proxies /api/* to the engine (see vite.config.js); in production the
// FastAPI server mounts this bundle and serves the API from the same origin.
// Basic auth (when configured) is handled by the browser natively.
//
// Mutating routes additionally require the dashboard API token, which this
// module attaches from lib/auth-token.js (never from the served HTML). Read
// routes work without it, so the UI is never locked out.

import { tokenHeaders } from "./auth-token.js";

const JSON_HEADERS = { Accept: "application/json" };

/**
 * Error kinds.
 *
 * `ENGINE_STARTING` is not a fault: the desktop shell puts the window on screen
 * (BOOT_TO_VISIBLE ~0.17s) before the engine sidecar answers (~0.69s), so the
 * first calls of a launch legitimately fail in one of two shapes —
 *
 *   * nothing is listening on the engine port yet, so `fetch` throws, or
 *   * the window is still on the shell's own asset origin, which answers
 *     `/api/*` with the SPA's HTML, so the body is not the engine's JSON.
 *
 * Neither means "this view is broken", so they are classified apart from a
 * real refusal and rendered as the calm starting state (see
 * lib/engineStartup.js). `HTTP` covers everything the engine actually answered.
 */
export const API_ERROR_ENGINE_STARTING = "engine-starting";
export const API_ERROR_HTTP = "http";

/**
 * Error carrying the HTTP status and the parsed JSON body.
 *
 * The first-run flow needs the *engine's* reason for a refusal (a 409 from
 * /api/post carries per-destination blockers), so the body is preserved
 * instead of being flattened into a status code string.
 */
export class ApiError extends Error {
  constructor(path, status, body, message, kind = API_ERROR_HTTP) {
    super(message ?? `${path} → HTTP ${status}`);
    this.name = "ApiError";
    this.path = path;
    this.status = status;
    this.body = body ?? null;
    this.kind = kind;
  }

  /** Engine-provided detail string, when present. */
  get detail() {
    const detail = this.body?.detail;
    return typeof detail === "string" ? detail : undefined;
  }

  /** True while the engine has not answered yet — a launch, not a failure. */
  get engineStarting() {
    return this.kind === API_ERROR_ENGINE_STARTING;
  }
}

async function readBody(res) {
  const type = res.headers.get("content-type") ?? "";
  if (!type.includes("json")) return null;
  try {
    return await res.json();
  } catch {
    return null;
  }
}

async function requestJSON(path, options = {}) {
  let res;
  try {
    res = await fetch(path, options);
  } catch (cause) {
    throw new ApiError(
      path,
      0,
      null,
      `${path} → engine unreachable (${cause?.message ?? cause})`,
      API_ERROR_ENGINE_STARTING
    );
  }
  if (!res.ok) {
    const body = await readBody(res);
    const detail = typeof body?.detail === "string" ? body.detail : undefined;
    throw new ApiError(path, res.status, body, detail ?? `${path} → HTTP ${res.status}`);
  }
  const body = await readBody(res);
  if (body === null) {
    // 2xx without the engine's JSON: the window is still on the shell's asset
    // origin, so the engine has not taken over yet. Not a broken view.
    throw new ApiError(
      path,
      res.status,
      null,
      `${path} → response was not JSON`,
      API_ERROR_ENGINE_STARTING
    );
  }
  return body;
}

/** True when a failed call only means the engine has not answered yet. */
export function isEngineStarting(error) {
  return Boolean(error && typeof error === "object" && error.engineStarting === true);
}

/**
 * Copy for a real failure. `ApiError.message` stays the technical string (paths,
 * statuses) for logs and tests; this is what a person reads, so it never
 * contains a route path or a "response was not JSON" style internal.
 */
export function errorMessage(cause) {
  if (cause instanceof ApiError) {
    // The engine's own detail is written for a person ("no video file found",
    // per-destination blockers) — prefer it over any generic sentence.
    if (cause.detail) return cause.detail;
    if (cause.engineStarting) {
      return "The app is still connecting to its local engine. Give it a moment, then retry.";
    }
    if (cause.status === 401 || cause.status === 403) {
      return "xPST refused this action without its dashboard token. Reopen the desktop app and try again.";
    }
    if (cause.status >= 500) {
      return "The local engine hit an error answering this view. Restart xPST; if it keeps happening, run Diagnostics.";
    }
    return "The local engine refused this request. Reload the view, or restart xPST if it keeps happening.";
  }
  return cause instanceof Error ? cause.message : String(cause);
}

function getJSON(path) {
  return requestJSON(path, { headers: { ...JSON_HEADERS, ...tokenHeaders() } });
}

function postJSON(path, payload) {
  return requestJSON(path, {
    method: "POST",
    headers: { ...JSON_HEADERS, ...tokenHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify(payload ?? {}),
  });
}

export const api = {
  /** Aggregate summary stats for dashboard cards. */
  summary: () => getJSON("/api/summary"),
  /**
   * Per-post/per-platform outcomes, labelled recorded vs live. Pass
   * `live=true` to run a real collection first (slower; uses API quota).
   */
  outcomeReport: (live = false) => getJSON(`/api/analytics/outcomes${live ? "?live=1" : ""}`),
  /** Per-video list for the Videos page. */
  videos: () => getJSON("/api/videos"),
  /** Engine health + live auth liveness per platform. */
  healthStatus: () => getJSON("/api/health-status"),
  /** Local no-network post preflight. */
  preflight: (payload) => postJSON("/api/preflight", payload),
  /** Masked config sections for the Settings page. */
  settings: () => getJSON("/api/settings"),
  /** Persisted schedule entries (read-only; never starts the scheduler). */
  schedules: () => getJSON("/api/schedules"),
  /** Renew due/expiring tokens now (bounded retry; no-op when nothing is due). */
  refreshTokens: () => postJSON("/api/refresh-tokens", {}),
  /** Recorded posting failures with truthful recovery metadata. */
  activity: () => getJSON("/api/activity"),
  /** Verified local library items. */
  library: () => getJSON("/api/library"),
  /** Role-aware provider catalog from the canonical backend contract. */
  providers: () => getJSON("/api/providers"),

  // ── First-run flow ────────────────────────────────────────────────
  /** First-run state: source folder, destinations, readiness, next step. */
  onboarding: () => getJSON("/api/onboarding"),
  /** Persist the content folder and the enabled destinations. */
  saveOnboarding: (payload) => postJSON("/api/onboarding", payload),
  /** Persist the "onboarding finished" flag so the wizard is offered once. */
  completeOnboarding: () => postJSON("/api/onboarding/complete", {}),
  /** Local media files in a folder (defaults to the configured folder). */
  media: (folder = "") => getJSON(`/api/media${folder ? `?folder=${encodeURIComponent(folder)}` : ""}`),
  /** Inspect / enable / verify one destination platform. */
  connect: (platform, payload = {}) => postJSON(`/api/connect/${encodeURIComponent(platform)}`, payload),

  // ── In-app sign-in (the Sign in control) ─────────────────────────
  /** Start the xPST-owned OAuth flow for a platform (opens the consent page). */
  startSignIn: (platform, payload = {}) =>
    postJSON(`/api/auth/signin/${encodeURIComponent(platform)}`, payload),
  /** Poll a sign-in session; each poll advances the engine's state machine. */
  signInStatus: (sessionId) => getJSON(`/api/auth/signin/${encodeURIComponent(sessionId)}`),
  /** Abandon a sign-in session (no credential is written). */
  cancelSignIn: (sessionId) => postJSON(`/api/auth/signin/${encodeURIComponent(sessionId)}/cancel`, {}),
  /** Every live (non-terminal) sign-in session. */
  signIns: () => getJSON("/api/auth/signin"),
  /** Plan (dry_run: true) or run a post through the real engine path. */
  post: (payload) => postJSON("/api/post", payload),

  // ── Durable drafts ───────────────────────────────────────────────
  /** Stored drafts, newest first, each revalidated against this machine now. */
  drafts: () => getJSON("/api/drafts"),
  /** Create or update the current draft (autosave). */
  saveDraft: (payload) => postJSON("/api/drafts", payload),
  /** One draft plus its fresh verdict (used when a screen resumes). */
  draft: (draftId) => getJSON(`/api/drafts/${encodeURIComponent(draftId)}`),
  /** Discard a draft. */
  deleteDraft: (draftId) =>
    requestJSON(`/api/drafts/${encodeURIComponent(draftId)}`, {
      method: "DELETE",
      headers: { ...JSON_HEADERS, ...tokenHeaders() },
    }),
};

// ── Hash routing ──────────────────────────────────────────────────────
// Routes mirror the QML desktop page ids so both shells share one set of
// section names. Index route is "dashboard"; anything unknown falls back
// to Dashboard in App.svelte.

export const NAV_ITEMS = [
  { id: "dashboard", href: "#/", label: "Dashboard", icon: "layout-dashboard" },
  { id: "onboarding", href: "#/onboarding", label: "Setup", icon: "sparkles" },
  { id: "connect", href: "#/connect", label: "Connect", icon: "plug" },
  { id: "compose", href: "#/compose", label: "Compose", icon: "clapperboard" },
  { id: "create", href: "#/create", label: "Preflight", icon: "square-pen" },
  { id: "result", href: "#/result", label: "Last post", icon: "list-checks" },
  { id: "analytics", href: "#/analytics", label: "Analytics", icon: "chart-no-axes-combined" },
  { id: "videos", href: "#/videos", label: "Videos", icon: "video" },
  { id: "accounts", href: "#/accounts", label: "Accounts", icon: "users" },
  { id: "schedule", href: "#/schedule", label: "Schedule", icon: "calendar-clock" },
  { id: "activity", href: "#/activity", label: "Activity", icon: "triangle-alert" },
  { id: "library", href: "#/library", label: "Library", icon: "library" },
  { id: "about", href: "#/about", label: "About", icon: "info" },
  { id: "settings", href: "#/settings", label: "Settings", icon: "settings" },
];

/** Current route id derived from a hash ("dashboard" by default). */
export function currentRoute(inputHash = undefined) {
  const hash = String(
    inputHash ?? (typeof location === "undefined" ? "" : location.hash) ?? ""
  ).replace(/^#\/?/, "");
  return NAV_ITEMS.some((item) => item.id === hash) ? hash : "dashboard";
}

/** True when a hash points at the default landing route (no explicit section). */
export function isLandingHash(inputHash = undefined) {
  const raw = String(inputHash ?? (typeof location === "undefined" ? "" : location.hash) ?? "");
  return raw === "" || raw === "#" || raw === "#/";
}