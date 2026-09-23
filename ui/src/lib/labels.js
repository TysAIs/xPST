const PLATFORM_LABELS = {
  youtube: "YouTube",
  instagram: "Instagram",
  x: "X",
  tiktok: "TikTok",
  threads: "Threads",
  facebook: "Facebook Page",
  messenger: "Messenger",
  local: "Local",
  other: "Other",
};

const STATUS_LABELS = {
  success: "Success",
  ok: "OK",
  healthy: "Healthy",
  warning: "Warning",
  degraded: "Degraded",
  danger: "Error",
  error: "Error",
  invalid: "Not valid",
  disabled: "Disabled",
  unknown: "Unknown",
};

// One platform can hold several roles (source / video_destination /
// analytics). A readiness row is identified by (platform, role), so the role
// must be rendered: three rows that all read "Instagram" are indistinguishable
// and read as a rendering bug.
const ROLE_LABELS = {
  source: "Source",
  video_destination: "Destination",
  analytics: "Analytics",
  messaging: "Messaging",
};

function titleCase(value, fallback) {
  const normalized = String(value ?? "").trim().replace(/[_-]+/g, " ");
  if (!normalized) return fallback;
  return normalized.replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function platformLabel(value) {
  const normalized = String(value ?? "other").trim().toLowerCase();
  return PLATFORM_LABELS[normalized] ?? titleCase(normalized, "Other");
}

export function statusLabel(value) {
  const normalized = String(value ?? "unknown").trim().toLowerCase();
  return STATUS_LABELS[normalized] ?? titleCase(normalized, "Unknown");
}

export function roleLabel(value) {
  const normalized = String(value ?? "").trim().toLowerCase();
  return ROLE_LABELS[normalized] ?? titleCase(normalized, "Unknown role");
}

/**
 * The readiness pill/copy pair for a readiness payload.
 *
 * The engine already decided it: `readiness.verdict` is built once
 * (src/xpst/readiness.py) and served by BOTH /api/health-status and
 * /api/onboarding, so the Home panel, the setup wizard and the payloads they
 * render from cannot disagree. Only a payload without the field gets a
 * fallback — no screen invents a second verdict.
 */
export function readinessVerdict(readiness) {
  const verdict = readiness?.verdict;
  if (verdict?.label) {
    return {
      status: verdict.status ?? "unknown",
      label: verdict.label,
      detail: verdict.detail ?? "",
    };
  }
  if (readiness?.pending) {
    return {
      status: "unknown",
      label: "Checking…",
      detail: "Live account checks are still running. Nothing is claimed until they answer.",
    };
  }
  const blockers = (readiness?.blockers ?? []).length;
  const warnings = (readiness?.warnings ?? []).length;
  if (!readiness?.ready || blockers) {
    return {
      status: "degraded",
      label: "Needs attention",
      detail: readiness?.summary ?? "At least one enabled role needs attention.",
    };
  }
  if (warnings) {
    return { status: "warning", label: "Ready with warnings", detail: readiness?.summary ?? "" };
  }
  return { status: "healthy", label: "Ready", detail: readiness?.summary ?? "Ready to post." };
}
