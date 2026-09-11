// Thin fetch client over the xPST engine /api endpoints plus the hash-router
// helpers the App shell needs. Same-origin relative paths: in dev, Vite
// proxies /api/* to the engine (see vite.config.js); in production the
// FastAPI server mounts this bundle and serves the API from the same origin.
// Basic auth (when configured) is handled by the browser natively.

const JSON_HEADERS = { Accept: "application/json" };

async function getJSON(path) {
  const res = await fetch(path, { headers: JSON_HEADERS });
  if (!res.ok) throw new Error(`${path} → HTTP ${res.status}`);
  return res.json();
}

export const api = {
  /** Aggregate summary stats for dashboard cards. */
  summary: () => getJSON("/api/summary"),
  /** Per-video list for the Videos page. */
  videos: () => getJSON("/api/videos"),
  /** Engine health + live auth liveness per platform. */
  healthStatus: () => getJSON("/api/health-status"),
  /** Canonical provider roles and capability readiness. */
  providers: () => getJSON("/api/providers"),
  /** Masked config sections for the Settings page. */
  settings: () => getJSON("/api/settings"),
};

// ── Hash routing ──────────────────────────────────────────────────────
// Routes mirror the QML desktop page ids so both shells share one set of
// section names. Index route is "dashboard"; anything unknown falls back
// to Dashboard in App.svelte.

export const NAV_ITEMS = [
  { id: "dashboard", href: "#/", label: "Dashboard", icon: "layout-dashboard" },
  { id: "analytics", href: "#/analytics", label: "Analytics", icon: "chart-no-axes-combined" },
  { id: "videos", href: "#/videos", label: "Videos", icon: "video" },
  { id: "accounts", href: "#/accounts", label: "Accounts", icon: "users" },
  { id: "settings", href: "#/settings", label: "Settings", icon: "settings" },
];

/** Current route id derived from a hash ("dashboard" by default). */
export function currentRoute(inputHash = undefined) {
  const hash = String(
    inputHash ?? (typeof location === "undefined" ? "" : location.hash) ?? ""
  ).replace(/^#\/?/, "");
  return NAV_ITEMS.some((item) => item.id === hash) ? hash : "dashboard";
}
