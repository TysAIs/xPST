const PLATFORM_LABELS = {
  youtube: "YouTube",
  instagram: "Instagram",
  x: "X",
  tiktok: "TikTok",
  threads: "Threads",
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
