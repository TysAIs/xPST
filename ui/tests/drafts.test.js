// Draft logic tests: what the compose screen keeps, what it restores, and how
// it words a stale plan.
//
// The rule under test: a plan that no longer matches the machine is never
// presented as ready, a refusal always says why, and a posted draft is never
// resurrected as in-progress work.

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { test } from "node:test";
import { compile } from "svelte/compiler";

import { api } from "../src/lib/api.js";
import {
  autosaveDecision,
  draftRequest,
  draftStatusLabel,
  draftStatusTone,
  emptyDraft,
  hasDraftContent,
  isStaleRefusal,
  newestDraft,
  planBanner,
  refusalText,
  restoreDraft,
  selectedPlatforms,
} from "../src/lib/drafts.js";

const UI_ROOT = resolve(import.meta.dirname, "..");

async function text(path) {
  return readFile(path, "utf8");
}

const DRAFT_ROW = {
  draft: {
    id: "draft_abc123",
    status: "planned",
    media_paths: ["/tmp/clips/one.mp4"],
    caption: "half-written",
    platforms: ["youtube", "x"],
    plan: { ready: true, recorded_at: "2026-09-15T10:00:00+00:00" },
  },
  verdict: {
    draft_id: "draft_abc123",
    planned: true,
    plan_ready: true,
    stale: false,
    reasons: [],
    validated_at: "2026-09-15T10:00:00+00:00",
  },
};

// ── The API client contract ──────────────────────────────────────────────

test("the api client exposes the draft endpoints the compose screen uses", () => {
  for (const name of ["drafts", "saveDraft", "draft", "deleteDraft"]) {
    assert.equal(typeof api[name], "function", `api.${name} is missing`);
  }
});

// ── Draft content ────────────────────────────────────────────────────────

test("a draft request carries only what the engine stores", () => {
  const request = draftRequest({ draftId: "draft_1", mediaPath: "/tmp/a.mp4", caption: "hi", platforms: { youtube: true, x: false } });
  assert.deepEqual(request, {
    draft_id: "draft_1",
    media_paths: ["/tmp/a.mp4"],
    caption: "hi",
    platforms: ["youtube"],
  });

  const fresh = draftRequest({ caption: "", platforms: {} });
  assert.equal("draft_id" in fresh, false, "a new draft must not invent an id");
  assert.deepEqual(fresh.media_paths, []);
});

test("selectedPlatforms lists only checked destinations", () => {
  assert.deepEqual(selectedPlatforms({ youtube: true, x: false, instagram: true }), ["youtube", "instagram"]);
  assert.deepEqual(selectedPlatforms(undefined), []);
});

test("an empty draft is not persisted", () => {
  assert.equal(hasDraftContent(emptyDraft()), false);
  assert.equal(hasDraftContent({ caption: "typed something", platforms: {} }), true);
  assert.equal(hasDraftContent({ mediaPath: "/tmp/a.mp4", platforms: {} }), true);
  assert.equal(hasDraftContent({ platforms: { youtube: true } }), true);
  assert.equal(emptyDraft().draftId, "");
});

// ── Restore on resume ────────────────────────────────────────────────────

test("the newest in-progress draft is restored with its verdict", () => {
  const payload = { drafts: [DRAFT_ROW, { draft: { id: "draft_older", status: "draft" }, verdict: null }] };
  const row = newestDraft(payload);
  assert.equal(row.draft.id, "draft_abc123");

  const restored = restoreDraft(row);
  assert.equal(restored.draftId, "draft_abc123");
  assert.equal(restored.mediaPath, "/tmp/clips/one.mp4");
  assert.equal(restored.caption, "half-written");
  assert.deepEqual(restored.platforms, { youtube: true, x: true });
  assert.equal(restored.verdict.stale, false);
  assert.equal(restored.restored, true);
});

test("a posted draft is never resumed as in-progress work", () => {
  const payload = { drafts: [{ draft: { id: "draft_done", status: "posted" }, verdict: null }] };
  assert.equal(newestDraft(payload), null);
  assert.equal(newestDraft({ drafts: [] }), null);
  assert.equal(newestDraft(null), null);
});

test("restoring an empty or malformed payload yields an empty draft", () => {
  assert.deepEqual(restoreDraft(null), emptyDraft());
  assert.deepEqual(restoreDraft({}), emptyDraft());
  assert.deepEqual(restoreDraft({ draft: { id: "x" } }), {
    draftId: "x",
    mediaPath: "",
    caption: "",
    platforms: {},
    verdict: null,
    restored: true,
  });
});

// ── Auto-save decisions ──────────────────────────────────────────────────

test("auto-save skips unchanged and empty states", () => {
  const before = { draftId: "draft_1", mediaPath: "/tmp/a.mp4", caption: "hi", platforms: { youtube: true } };
  assert.equal(autosaveDecision(before, { ...before }).save, false);
  assert.equal(autosaveDecision(before, { ...before, caption: "hi there" }).save, true);
  assert.equal(autosaveDecision(emptyDraft(), emptyDraft()).save, false);
  assert.equal(autosaveDecision(before, emptyDraft()).reset, true);
});

// ── Wording ──────────────────────────────────────────────────────────────

test("a stale plan is never labelled ready and lists the engine's reasons", () => {
  const verdict = {
    planned: true,
    plan_ready: true,
    stale: true,
    reasons: [{ code: "DESTINATION_NOT_READY", message: "youtube can no longer publish locally (missing)." }],
    validated_at: "2026-09-15T10:00:00+00:00",
  };
  const banner = planBanner(verdict);
  assert.equal(banner.show, true);
  assert.equal(banner.tone, "warning");
  assert.equal(banner.requiresReconfirmation, true);
  assert.match(banner.title, /out of date/i);
  assert.equal(draftStatusLabel(verdict), "Plan out of date");
  assert.equal(draftStatusTone(verdict), "warning");
  assert.notEqual(banner.tone, "success");
});

test("a current plan is shown as current; an unplanned draft shows nothing", () => {
  const current = planBanner(DRAFT_ROW.verdict);
  assert.equal(current.show, true);
  assert.equal(current.tone, "success");
  assert.equal(current.requiresReconfirmation, false);
  assert.equal(draftStatusLabel(DRAFT_ROW.verdict), "Plan is current");

  assert.equal(planBanner(null).show, false);
  assert.equal(planBanner({ planned: false, stale: false, reasons: [] }).show, false);
  assert.equal(draftStatusLabel(null), "Draft");
});

test("a blocked-but-current plan reports the engine's blockers", () => {
  const banner = planBanner({ planned: true, plan_ready: false, stale: false, plan_blockers: ["Choose a video file before posting."] });
  assert.equal(banner.show, true);
  assert.equal(banner.tone, "error");
  assert.match(banner.detail, /Choose a video file/);
});

test("a stale refusal is recognised and its reasons quoted verbatim", () => {
  const body = {
    stale: true,
    stale_reasons: [{ code: "MEDIA_MISSING", message: "The media file /tmp/a.mp4 is no longer on disk." }],
    blockers: ["The media file /tmp/a.mp4 is no longer on disk."],
  };
  assert.equal(isStaleRefusal(body), true);
  assert.equal(isStaleRefusal({ ok: true }), false);
  assert.equal(isStaleRefusal(null), false);
  assert.match(refusalText(body), /is no longer on disk/);

  assert.match(refusalText({ unknown_draft: true }), /no longer stored on disk/);
  assert.match(refusalText({ blockers: ["no destination"] }), /no destination/);
  assert.equal(refusalText(null), "The engine refused this post.");
});

// ── The screen itself ────────────────────────────────────────────────────

test("the compose screen saves, restores, and revalidates through the engine", async () => {
  const compose = await text(join(UI_ROOT, "src/pages/Compose.svelte"));
  assert.match(compose, /api\.drafts\(\)/, "compose does not load stored drafts");
  assert.match(compose, /api\.saveDraft\(/, "compose does not auto-save");
  assert.match(compose, /api\.draft\(draftId\)/, "compose does not revalidate the resumed draft");
  assert.match(compose, /newestDraft/, "compose does not pick the newest draft");
  assert.match(compose, /confirm_stale/, "compose has no re-confirmation path");
  assert.match(compose, /draft_id/, "compose does not bind its requests to the draft");
});

test("the compose screen never lets a late restore overwrite live typing", async () => {
  const compose = await text(join(UI_ROOT, "src/pages/Compose.svelte"));
  assert.match(compose, /userEdited/, "no guard against a late restore clobbering user input");
  assert.match(compose, /if \(!userEdited\)/, "the restore is not guarded");
});

test("the compose screen still compiles with the Svelte compiler", async () => {
  const source = await text(join(UI_ROOT, "src/pages/Compose.svelte"));
  const { js, warnings } = compile(source, { filename: "Compose.svelte", generate: "client" });
  assert.ok(js.code.length > 0);
  const fatal = (warnings ?? []).filter((warning) => warning.code === "missing-declaration");
  assert.deepEqual(fatal, [], `Compose has undeclared references: ${JSON.stringify(fatal)}`);
});
