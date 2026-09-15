// Thin fetch client over the xPST engine /api endpoints plus the hash-router
// helpers the App shell needs. Same-origin relative paths: in dev, Vite
// proxies /api/* to the engine (see vite.config.js); in production the
// FastAPI server mounts this bundle and serves the API from the same origin.
// Basic auth (when configured) is handled by the browser natively.

const JSON_HEADERS = { Accept: "application/json" };

/**
 * Error carrying the HTTP status and the parsed JSON body.
 *
 * The first-run flow needs the *engine's* reason for a refusal (a 409 from
 * /api/post carries per-destination blockers), so the body is preserved
 * instead of being flattened into a status code string.
 */
export class ApiError extends Error {
  constructor(path, status, body, message) {
    super(message ?? `${path} → HTTP ${status}`);
    this.name = "ApiError";
    this.path = path;
    this.status = status;
    this.body = body ?? null;
  }

  /** Engine-provided detail string, when present. */
  get detail() {
    const detail = this.body?.detail;
    return typeof detail === "string" ? detail : undefined;
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
    throw new ApiError(path, 0, null, `${path} → engine unreachable (${cause?.message ?? cause})`);
  }
  if (!res.ok) {
    const body = await readBody(res);
    const detail = typeof body?.detail === "string" ? body.detail : undefined;
    throw new ApiError(path, res.status, body, detail ?? `${path} → HTTP ${res.status}`);
  }
  const body = await readBody(res);
  if (body === null) throw new ApiError(path, res.status, null, `${path} → response was not JSON`);
  return body;
}

function getJSON(path) {
  return requestJSON(path, { headers: JSON_HEADERS });
}

function postJSON(path, payload) {
  return requestJSON(path, {
    method: "POST",
    headers: { ...JSON_HEADERS, "Content-Type": "application/json" },
    body: JSON.stringify(payload ?? {}),
  });
}

export const api = {
  /** Aggregate summary stats for dashboard cards. */
  summary: () => getJSON("/api/summary"),
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
    requestJSON(`/api/drafts/${encodeURIComponent(draftId)}`, { method: "DELETE", headers: JSON_HEADERS }),
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