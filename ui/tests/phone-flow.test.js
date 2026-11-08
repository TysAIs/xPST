// The phone flow's render contract (docs/PHONE-FIRST-UX.md §2 and §7).
//
// These tests protect the two things a screenshot cannot: that the labels come
// from the engine's own vocabulary (a refused destination never renders as
// "Publish"), and that the screen really is built for a 390 px phone with 44 px
// targets. They also compile the component, so a Svelte syntax error fails the
// node gate instead of only the build.

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { test } from "node:test";
import { compile } from "svelte/compiler";

import {
  MIN_TARGET_PX,
  PHONE_STEPS,
  PHONE_VIEWPORT_PX,
  confirmLabel,
  destinationDecision,
  needsPairing,
  platformLabel,
  previewRows,
  planFromHash,
  resumeStep,
  probeSummary,
  staleReasons,
  stepIndex,
  uploadIdempotencyKey,
  verificationBadge,
} from "../src/lib/phone.js";

const UI_ROOT = resolve(import.meta.dirname, "..");

async function text(path) {
  return readFile(join(UI_ROOT, path), "utf8");
}

test("the flow is six ordered steps and none can be jumped", () => {
  assert.deepEqual(
    PHONE_STEPS.map((step) => step.id),
    ["compose", "criteria", "preview", "confirm", "publish", "readback"]
  );
  assert.equal(stepIndex("compose"), 0);
  assert.equal(stepIndex("readback"), 5);
  assert.equal(stepIndex("nope"), -1);
});

test("the acceptance viewport is 390 px with 44 px targets", () => {
  assert.equal(PHONE_VIEWPORT_PX, 390);
  assert.equal(MIN_TARGET_PX, 44);
});

test("a destination the engine refused never renders as publish", () => {
  const rows = previewRows({
    route: [
      { platform: "youtube", state: "posted", will_post: true },
      { platform: "x", state: "skipped_unconnected", will_post: false, reason: "AUTH_NOT_READY" },
      { platform: "instagram", state: "unsupported_format", will_post: false, reason: "video too long" },
      { platform: "tiktok", state: "failed", will_post: false, reason: "upload crashed" },
    ],
  });
  assert.equal(rows[0].tone, "success");
  assert.match(rows[0].label, /Publish/);
  assert.equal(rows[1].tone, "warning");
  assert.match(rows[1].label, /Skipped, not connected/);
  assert.equal(rows[2].tone, "danger");
  assert.match(rows[2].label, /Refused, format/);
  assert.match(rows[2].detail, /too long/);
  assert.equal(rows[3].tone, "danger");
  // An unknown state is shown, not coerced into success.
  const unknown = destinationDecision({ platform: "threads", state: "needs_gate", will_post: false });
  assert.equal(unknown.tone, "warning");
  assert.match(unknown.label, /needs_gate/);
});

test("the confirm control names every destination it will publish to", () => {
  const preview = { route: [{ platform: "youtube", will_post: true }, { platform: "x", will_post: true }] };
  assert.equal(confirmLabel(preview), "Publish to YouTube and X");
  assert.equal(confirmLabel({ route: [{ platform: "x", will_post: true }] }), "Publish to X");
  assert.equal(
    confirmLabel({ route: [{ platform: "youtube", will_post: false, state: "skipped_unconnected" }] }),
    "Nothing to publish"
  );
});

test("verification labels never upgrade an unread id to verified", () => {
  assert.equal(verificationBadge({ verification: "verified" }).label, "Verified");
  const unconfirmed = verificationBadge({ verification: "submitted_unconfirmed", platform_id: "abc" });
  assert.equal(unconfirmed.label, "Submitted, unconfirmed");
  assert.equal(unconfirmed.tone, "warning");
  assert.equal(verificationBadge({ verification: "not_found" }).tone, "danger");
  assert.equal(verificationBadge({ verification: "failed", error: "boom" }).detail, "boom");
  assert.equal(verificationBadge({}).label, "Submitted, unconfirmed");
});

test("an unmeasured probe says so instead of showing zeros", () => {
  const unmeasured = probeSummary({ probed: false, probe_error: "ffprobe missing" });
  assert.equal(unmeasured.probed, false);
  assert.match(unmeasured.title, /not measured/i);
  assert.match(unmeasured.detail, /ffprobe missing/);
  const measured = probeSummary({
    probed: true,
    container: "mp4",
    kind: "video",
    bytes: 2 * 1024 * 1024,
    probe: { width: 390, height: 844, duration_s: 12.5, codec: "h264" },
  });
  assert.match(measured.title, /390×844/);
  assert.match(measured.title, /12.5s/);
  assert.match(measured.title, /2.0 MB/);
});

test("stale reasons are surfaced, and a 401 asks for pairing", () => {
  assert.deepEqual(staleReasons({ code: "PLAN_CONFIRM_STALE", stale_reasons: ["x: skipped_unconnected"] }), [
    "x: skipped_unconnected",
  ]);
  assert.deepEqual(staleReasons({ message: "changed" }), ["changed"]);
  assert.equal(needsPairing({ status: 401 }), true);
  assert.equal(needsPairing({ status: 409 }), false);
  assert.equal(platformLabel("youtube"), "YouTube");
});

test("a retried upload of the same file reuses its idempotency key", () => {
  const file = { name: "clip.mp4", size: 1234, lastModified: 42 };
  assert.equal(uploadIdempotencyKey(file), "phone:clip.mp4:1234:42");
  assert.equal(uploadIdempotencyKey(file), uploadIdempotencyKey({ ...file }));
});

test("the Phone page compiles and carries the 44 px / 390 px contract", async () => {
  const source = await text("src/pages/Phone.svelte");
  const { js } = compile(source, { filename: "Phone.svelte", generate: "dom" });
  assert.ok(js.code.length > 0);
  assert.match(source, /min-height: var\(--phone-target\)/);
  assert.match(source, /max-width: var\(--phone-width\)/);
  assert.match(source, /overflow-x: hidden/);
  // Every step is rendered from the shared step list, not a copied literal.
  assert.match(source, /PHONE_STEPS/);
  assert.match(source, /confirmLabel\(/);
  assert.match(source, /verificationBadge\(/);
  // The flow never skips straight from compose to publish.
  assert.match(source, /step === "criteria"/);
  assert.match(source, /step === "preview" \|\| step === "confirm"/);
  assert.match(source, /step === "readback"/);
});

test("the route and the API client are wired", async () => {
  const app = await text("src/App.svelte");
  assert.match(app, /from "\.\/pages\/Phone\.svelte"/);
  assert.match(app, /phone: Phone/);
  const api = await text("src/lib/api.js");
  for (const name of [
    "mediaUpload",
    "pairDevice",
    "createPlan",
    "confirmPlan",
    "publishPlan",
    "planReadback",
  ]) {
    assert.match(api, new RegExp(`${name}:`));
  }
  assert.match(api, /href: "#\/phone"/);
});

test("a cooled-down destination renders as deferred with its window, never failed", async () => {
  const { deferredDetail } = await import("../src/lib/phone.js");
  const row = {
    platform: "x",
    verification: "deferred",
    state: "deferred",
    resume_after: "2026-11-07T23:45:00",
    verification_reason: "RATE_LIMIT_COOLDOWN: x cooling, resume after 2026-11-07T23:45:00",
  };
  const badge = verificationBadge(row);
  assert.equal(badge.tone, "warning");
  assert.equal(badge.label, "Deferred");
  assert.match(badge.detail, /23:45/);
  // The preview row says the same thing, and it is not a failure.
  const decision = destinationDecision({ platform: "x", state: "deferred", will_post: false, resume_after: "2026-11-07T23:45:00" });
  assert.equal(decision.tone, "warning");
  assert.match(decision.label, /Deferred/);
  assert.match(deferredDetail(row), /resume after 2026-11-07T23:45:00/);
  // A refusal is named, not collapsed into "failed".
  assert.equal(verificationBadge({ verification: "refused" }).label, "Refused");
});

test("a resumed plan lands on the engine's true state, and the URL carries the plan", async () => {
  const { planFromHash, resumeStep } = await import("../src/lib/phone.js");
  const { currentRoute } = await import("../src/lib/api.js");
  assert.equal(planFromHash("#/phone?plan=plan_ab12"), "plan_ab12");
  assert.equal(planFromHash("#/phone"), "");
  assert.equal(planFromHash(""), "");
  // The router still resolves a route that carries a query.
  assert.equal(currentRoute("#/phone?plan=plan_ab12"), "phone");
  assert.equal(currentRoute("#/phone"), "phone");
  assert.equal(currentRoute("#/nope"), "dashboard");
  // A plan that ran goes to the readback; a previewed one to the preview.
  assert.equal(resumeStep({ plan_id: "plan_1", state: "published", result: { ok: true } }), "readback");
  assert.equal(resumeStep({ plan_id: "plan_2", state: "confirmed", result: { ok: false, deferred_count: 1 } }), "readback");
  assert.equal(resumeStep({ plan_id: "plan_3", state: "previewed", preview: {} }), "preview");
  assert.equal(resumeStep({ plan_id: "plan_4", state: "confirmed" }), "publish");
  assert.equal(resumeStep({ plan_id: "plan_5", state: "confirmed", dry_run_result: { ok: true } }), "publish");
  assert.equal(resumeStep(null), "compose");
  const source = await readFile(join(UI_ROOT, "src/pages/Phone.svelte"), "utf8");
  assert.match(source, /rememberPlan\(plan\.plan_id\)/);
  assert.match(source, /onMount\(resumePlan\)/);
});
