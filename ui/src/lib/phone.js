// View models for the phone publish flow (docs/PHONE-FIRST-UX.md §2).
//
// The flow is six steps and every one of them renders an object the engine
// owns: the probed media record, the resolved route plan, the frozen plan, the
// per-destination result and the readback. Nothing here decides capability or
// verification — it only turns the engine's answer into the one label the
// screen may show. That is why these helpers are pure and unit-tested: a
// destination that the engine refused must never render as "Publish".

export const PHONE_VIEWPORT_PX = 390;
export const MIN_TARGET_PX = 44;

export const PHONE_STEPS = [
  { id: "compose", label: "Compose", hint: "Pick the video and the caption" },
  { id: "criteria", label: "Destinations", hint: "Choose where this post goes" },
  { id: "preview", label: "Preview", hint: "What the engine will do, per destination" },
  { id: "confirm", label: "Confirm", hint: "Freeze the plan" },
  { id: "publish", label: "Publish", hint: "One plan, one run" },
  { id: "readback", label: "Readback", hint: "What each platform actually says" },
];

/** Index of a step id, or -1. Used to gate "next" controls, never to skip one. */
export function stepIndex(id) {
  return PHONE_STEPS.findIndex((step) => step.id === id);
}

/** Human label for a platform name (matches the rest of the UI). */
export function platformLabel(name) {
  const key = String(name ?? "").toLowerCase();
  const labels = {
    youtube: "YouTube",
    x: "X",
    instagram: "Instagram",
    threads: "Threads",
    facebook: "Facebook",
    tiktok: "TikTok",
    rumble: "Rumble",
  };
  return labels[key] ?? (key ? key.charAt(0).toUpperCase() + key.slice(1) : "Destination");
}

/**
 * The probed format of a staged upload, in the words the compose screen shows.
 * Values the probe could not measure are reported as "not measured" with the
 * engine's reason — never as zero and never omitted.
 */
export function probeSummary(media) {
  if (!media) {
    return { probed: false, title: "No media yet", detail: "Pick a video from the camera roll.", reasons: [] };
  }
  const probe = media.probe ?? {};
  const parts = [];
  if (probe.width && probe.height) parts.push(`${probe.width}×${probe.height}`);
  if (probe.duration_s) parts.push(`${Number(probe.duration_s).toFixed(1)}s`);
  if (probe.codec) parts.push(probe.codec);
  if (media.bytes) parts.push(formatBytes(media.bytes));
  const probed = Boolean(media.probed);
  return {
    probed,
    container: media.container ?? "",
    title: probed ? parts.join(" · ") || media.container || "probed" : "Format not measured",
    detail: probed
      ? `${media.container ?? "container"} · ${media.kind ?? "media"}`
      : media.probe_error || "The probe could not read this file; the preview will still check the envelope.",
    reasons: probed ? [] : [media.probe_error].filter(Boolean),
  };
}

export function formatBytes(bytes) {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  if (value < 1024 * 1024) return `${Math.round(value / 1024)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * One destination row for the criteria and preview screens.
 *
 * The engine's canonical states decide the label; an unknown state is shown
 * verbatim rather than coerced into "Publish".
 */
export function destinationDecision(row) {
  const state = String(row?.state ?? "");
  const name = platformLabel(row?.platform);
  if (row?.will_post) {
    return { platform: row?.platform, label: `${name} — Publish`, tone: "success", detail: row?.reason || "" };
  }
  if (state === "skipped_unconnected") {
    return {
      platform: row?.platform,
      label: `${name} — Skipped, not connected`,
      tone: "warning",
      detail: row?.reason || row?.connect_command || "Connect this account to include it.",
    };
  }
  if (state === "unsupported_format") {
    return { platform: row?.platform, label: `${name} — Refused, format`, tone: "danger", detail: row?.reason || "" };
  }
  if (state === "deferred") {
    return {
      platform: row?.platform,
      label: `${name} — Deferred`,
      tone: "warning",
      detail: deferredDetail(row),
    };
  }
  if (state === "failed") {
    return { platform: row?.platform, label: `${name} — Failed`, tone: "danger", detail: row?.reason || "" };
  }
  return { platform: row?.platform, label: `${name} — ${state || "unknown"}`, tone: "warning", detail: row?.reason || "" };
}

/** The preview rows in plan order, with the decision each one carries. */
export function previewRows(preview) {
  const rows = preview?.route ?? preview?.route_plan?.destinations ?? [];
  return rows.map(destinationDecision);
}

/** Destinations the plan would actually publish to. */
export function publishablePlatforms(preview) {
  const rows = preview?.route ?? preview?.route_plan?.destinations ?? [];
  return rows.filter((row) => row?.will_post).map((row) => row.platform);
}

/**
 * The confirm button names what will happen: "Publish to YouTube and X".
 * A plan with nothing publishable says so instead of offering a dead button.
 */
export function confirmLabel(preview) {
  const names = publishablePlatforms(preview).map(platformLabel);
  if (!names.length) return "Nothing to publish";
  if (names.length === 1) return `Publish to ${names[0]}`;
  return `Publish to ${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/** The window a deferred destination is waiting for, in words. */
export function deferredDetail(row) {
  const window = row?.resume_after ? ` Resumes after ${row.resume_after}.` : "";
  const reason = row?.reason || row?.error || row?.verification_reason || "";
  return `${reason || "Nothing was uploaded yet; the destination is waiting for its window."}${window}`.trim();
}

/**
 * Verification state of a readback row, in the product's own vocabulary.
 * "Verified" requires the platform to have answered; an id nobody read back is
 * "Submitted, unconfirmed" and never a green tick.
 */
export function verificationBadge(row) {
  const state = String(row?.verification ?? "");
  if (state === "verified") {
    return { tone: "success", label: "Verified", detail: row?.verification_reason || "The platform returned this post." };
  }
  if (state === "not_found") {
    return { tone: "danger", label: "Not found on the platform", detail: row?.verification_reason || "" };
  }
  if (state === "failed") {
    return { tone: "danger", label: "Failed", detail: row?.error || row?.verification_reason || "" };
  }
  // A destination waiting out a cooldown/window did not fail: M0 requires the
  // deferred state and its window, never a red "failed" row.
  if (state === "deferred") {
    return { tone: "warning", label: "Deferred", detail: deferredDetail(row) };
  }
  if (state === "refused") {
    return { tone: "danger", label: "Refused", detail: row?.verification_reason || "" };
  }
  if (state === "not_attempted") {
    return { tone: "neutral", label: "Not attempted", detail: row?.verification_reason || "" };
  }
  return {
    tone: "warning",
    label: "Submitted, unconfirmed",
    detail: row?.verification_reason || "The engine recorded an id; no readback confirmed it.",
  };
}

/** Stable client key for one file, so a retry re-uses the upload instead of duplicating it. */
export function uploadIdempotencyKey(file) {
  if (!file) return "";
  const name = String(file.name ?? "upload");
  const size = Number(file.size ?? 0);
  const stamp = Number(file.lastModified ?? 0);
  return `phone:${name}:${size}:${stamp}`;
}

/**
 * The plan id a phone hash points at (`#/phone?plan=plan_ab12`), or "".
 * This is what makes the URL the operator reopens — or shares to a desktop —
 * show the same plan and the same readback instead of a fresh compose screen.
 */
export function planFromHash(hash) {
  const raw = String(hash ?? "");
  const query = raw.includes("?") ? raw.slice(raw.indexOf("?") + 1) : "";
  if (!query) return "";
  const params = new URLSearchParams(query);
  return String(params.get("plan") || "").trim();
}

/**
 * Which step a resumed plan belongs on. A plan that ran goes straight to the
 * readback (the engine's true state); a confirmed one lands on the publish card
 * (it is already frozen); a previewed one lands on the preview. Anything
 * unknown stays on compose rather than guessing.
 */
export function resumeStep(plan) {
  if (!plan || typeof plan !== "object") return "compose";
  // Only a REAL run resumes to the readback: a dry run uploaded nothing, so the
  // plan is still waiting for its tap and must land on the publish card.
  if (plan.result || plan.state === "published") return "readback";
  if (plan.state === "confirmed") return "publish";
  if (plan.plan_id) return "preview";
  return "compose";
}

/** Reasons out of a confirm_stale refusal body, as sentences. */
export function staleReasons(body) {
  const reasons = body?.stale_reasons ?? body?.error?.reasons ?? [];
  if (Array.isArray(reasons) && reasons.length) {
    return reasons.map((item) => (typeof item === "string" ? item : item?.message ?? String(item))).filter(Boolean);
  }
  if (body?.message) return [body.message];
  return [];
}

/** True when a failed call means this browser has no usable credential yet. */
export function needsPairing(cause) {
  return Boolean(cause && typeof cause === "object" && cause.status === 401);
}
