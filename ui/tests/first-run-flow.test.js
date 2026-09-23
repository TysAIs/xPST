// First-run flow tests: the truthful-result logic, the API client contract,
// and a compile check for every screen in the flow.
//
// The rule being protected here is the shipped defect this work fixes: a
// failed upload must never read as complete. Every assertion about "success"
// below is paired with a failure case that must never be reported as success.

import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { test } from "node:test";
import { compile } from "svelte/compiler";

import { ApiError, api, currentRoute, isLandingHash, NAV_ITEMS } from "../src/lib/api.js";
import {
  FLOW_ROUTES,
  destinationOutcomes,
  destinationRows,
  destinationStateLabel,
  formatBytes,
  postRequestSummary,
  readyDestinations,
  resultDescription,
  resultHeadline,
  resultTone,
  shouldStartOnboarding,
  stepRows,
  targetSummary,
} from "../src/lib/firstRun.js";
import {
  clearLastPost,
  emptyLastPost,
  getLastPost,
  setLastPost,
  subscribeLastPost,
} from "../src/lib/session.js";

const UI_ROOT = resolve(import.meta.dirname, "..");

async function text(path) {
  return readFile(path, "utf8");
}

async function exists(path) {
  try {
    await access(path);
    return true;
  } catch {
    return false;
  }
}

// ── Routing ──────────────────────────────────────────────────────────────

test("the first-run flow routes exist and are addressable by hash", () => {
  for (const route of FLOW_ROUTES) {
    assert.ok(NAV_ITEMS.some((item) => item.id === route.id), `${route.id} is not routable`);
    assert.equal(currentRoute(route.href), route.id);
  }
  assert.equal(isLandingHash(""), true);
  assert.equal(isLandingHash("#/"), true);
  assert.equal(isLandingHash("#/compose"), false);
});

test("onboarding is only auto-opened for a fresh install on the landing route", () => {
  const fresh = { first_run_complete: false };
  const returning = { first_run_complete: true };
  assert.equal(shouldStartOnboarding(fresh, ""), true);
  assert.equal(shouldStartOnboarding(fresh, "#/"), true);
  // An explicit navigation is never hijacked.
  assert.equal(shouldStartOnboarding(fresh, "#/analytics"), false);
  // A returning user, a failed load, and a malformed payload never redirect.
  assert.equal(shouldStartOnboarding(returning, ""), false);
  assert.equal(shouldStartOnboarding(null, ""), false);
  assert.equal(shouldStartOnboarding("nope", ""), false);
  assert.equal(shouldStartOnboarding({}, ""), false);
});

// ── Destination rows ─────────────────────────────────────────────────────

const ONBOARDING = {
  first_run_complete: false,
  steps: [
    { id: "welcome", title: "Welcome to xPST", done: true },
    { id: "source", title: "Choose a content folder", done: false },
    { id: "destination", title: "Connect a platform", done: false },
    { id: "ready", title: "Compose your first post", done: false },
  ],
  destinations: [
    {
      name: "youtube",
      display_name: "YouTube Shorts",
      enabled: true,
      auth_mode: "oauth",
      is_official_api: true,
      roles: ["source", "video_destination"],
      role_status: { video_destination: { state: "unconfigured", error: "YouTube OAuth token file is missing." } },
    },
    {
      name: "instagram",
      display_name: "Instagram Reels",
      enabled: false,
      auth_mode: "graph_api",
      roles: ["video_destination"],
      role_status: { video_destination: { state: "ready" } },
    },
    {
      name: "messenger",
      display_name: "Messenger",
      enabled: true,
      roles: ["messaging"],
      role_status: { messaging: { state: "ready" } },
    },
  ],
};

test("only video-destination providers become destination rows", () => {
  const rows = destinationRows(ONBOARDING);
  assert.deepEqual(rows.map((row) => row.name), ["youtube", "instagram"]);
  const youtube = rows[0];
  assert.equal(youtube.ready, false);
  assert.equal(youtube.enabled, true);
  assert.equal(youtube.stateLabel, "Not connected");
  assert.equal(youtube.error, "YouTube OAuth token file is missing.");
  // The catalog shape from /api/providers is accepted too.
  assert.deepEqual(destinationRows({ providers: ONBOARDING.destinations }).map((row) => row.name), ["youtube", "instagram"]);
  assert.deepEqual(destinationRows(null), []);
});

test("a ready destination is the only thing that counts as publishable", () => {
  const rows = destinationRows(ONBOARDING);
  assert.deepEqual(readyDestinations(ONBOARDING).map((row) => row.name), ["instagram"]);
  assert.equal(rows.find((row) => row.name === "youtube").ready, false);
  assert.equal(destinationStateLabel("blocked_external_review"), "Blocked by provider review");
  assert.equal(destinationStateLabel("something_new"), "something new");
});

test("wizard steps mark exactly one current step and never claim an unfinished one", () => {
  const steps = stepRows(ONBOARDING);
  assert.equal(steps.filter((step) => step.current).length, 1);
  assert.equal(steps.find((step) => step.current).id, "source");
  assert.equal(steps[0].done, true);
  assert.equal(steps[1].done, false);
  assert.deepEqual(stepRows(null), []);
});

test("target summary counts ready destinations and names the blocked ones", () => {
  const summary = targetSummary(destinationRows(ONBOARDING));
  assert.equal(summary.total, 2);
  assert.equal(summary.ready, 1);
  assert.deepEqual(summary.names, ["Instagram Reels"]);
  assert.deepEqual(summary.blocked, ["YouTube Shorts"]);
});

// ── Truthful post results (the regression this flow must not reintroduce) ──

const DRY_RUN_OK = {
  ok: true,
  dry_run: true,
  uploaded: false,
  requested: ["youtube"],
  uploaded_count: 0,
  failed_count: 0,
  blockers: [],
  destinations: [{ platform: "youtube", attempted: false, success: null, error: null }],
};

const REAL_SUCCESS = {
  ok: true,
  dry_run: false,
  uploaded: true,
  requested: ["youtube"],
  uploaded_count: 1,
  failed_count: 0,
  blockers: [],
  destinations: [{ platform: "youtube", attempted: true, success: true, post_url: "https://example.invalid/v/1" }],
};

// The shipped bug: an upload that failed was still logged "100% complete".
const REAL_FAILURE = {
  ok: false,
  dry_run: false,
  uploaded: false,
  requested: ["youtube", "instagram"],
  uploaded_count: 0,
  failed_count: 2,
  blockers: [],
  destinations: [
    { platform: "youtube", attempted: true, success: false, error: "upload rejected: 403" },
    { platform: "instagram", attempted: true, success: false, error: "Media processing failed after publish" },
  ],
};

const PARTIAL = {
  ok: false,
  dry_run: false,
  uploaded: true,
  requested: ["youtube", "instagram"],
  uploaded_count: 1,
  failed_count: 1,
  blockers: [],
  destinations: [
    { platform: "youtube", attempted: true, success: true, post_url: "https://example.invalid/v/2" },
    { platform: "instagram", attempted: true, success: false, error: "Instagram rejected the reel" },
  ],
};

const BLOCKED = {
  ok: false,
  dry_run: false,
  uploaded: false,
  requested: ["youtube"],
  uploaded_count: 0,
  failed_count: 1,
  blockers: ["YouTube OAuth token file is missing."],
  destinations: [{ platform: "youtube", attempted: true, success: false, error: "nothing uploaded" }],
};

test("a failed upload is never classified or worded as success", () => {
  assert.equal(resultTone(REAL_FAILURE), "failed");
  const headline = resultHeadline(REAL_FAILURE);
  assert.equal(headline, "Nothing was uploaded");
  assert.doesNotMatch(headline, /published|success|complete/i);
  assert.match(resultDescription(REAL_FAILURE), /failed/i);

  // The specific shipped defect: a failure must never read as a finished upload.
  for (const envelope of [REAL_FAILURE, BLOCKED]) {
    const words = `${resultHeadline(envelope)} ${resultDescription(envelope)}`;
    assert.doesNotMatch(words, /100%|complete|published to/i);
  }
});

test("only a real, fully published attempt is reported as success", () => {
  assert.equal(resultTone(REAL_SUCCESS), "success");
  assert.equal(resultHeadline(REAL_SUCCESS), "Published to 1 destination");
  assert.equal(resultTone(null), "idle");
  assert.equal(resultTone(REAL_FAILURE), "failed");
  assert.equal(resultTone({ ...REAL_FAILURE, uploaded_count: 1, failed_count: 1 }), "partial");
  assert.equal(resultTone(BLOCKED), "blocked");
  // A dry run can never be "success" even when the plan is ready.
  assert.equal(resultTone(DRY_RUN_OK), "dry-run-ready");
  assert.match(resultHeadline(DRY_RUN_OK), /nothing was uploaded/i);
});

test("each destination row reports its own outcome, including not-attempted", () => {
  const failureRows = destinationOutcomes(REAL_FAILURE);
  assert.deepEqual(failureRows.map((row) => row.label), ["Failed", "Failed"]);
  assert.equal(failureRows[1].detail, "Media processing failed after publish");

  const successRows = destinationOutcomes(REAL_SUCCESS);
  assert.equal(successRows[0].success, true);
  assert.equal(successRows[0].status, "success");

  const dryRows = destinationOutcomes(DRY_RUN_OK);
  assert.equal(dryRows[0].attempted, false);
  assert.equal(dryRows[0].success, false);
  assert.equal(dryRows[0].status, "disabled");
  assert.match(dryRows[0].label, /Not attempted/);

  assert.deepEqual(destinationOutcomes(null), []);
});

test("the request summary keeps only what was asked for", () => {
  assert.deepEqual(
    postRequestSummary({ media_paths: ["a.mp4"], caption: "hi", platforms: ["youtube"], dry_run: false, secret: "x" }),
    { media_paths: ["a.mp4"], caption: "hi", overrides: {}, captions: {}, platforms: ["youtube"], dry_run: false }
  );
  assert.equal(postRequestSummary(null), null);
  assert.equal(formatBytes(2048), "2.0 KB");
  assert.equal(formatBytes(1342177), "1.3 MB");
  assert.equal(formatBytes(0), "0 B");
});

// ── API client ───────────────────────────────────────────────────────────

function fetchRecorder(handler) {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, options });
    return handler(url, options, calls.length);
  };
  return calls;
}

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

test("the client exposes the whole first-run surface and uses the right verbs", async () => {
  for (const name of ["onboarding", "saveOnboarding", "completeOnboarding", "media", "connect", "post", "preflight"]) {
    assert.equal(typeof api[name], "function", `api.${name} is missing`);
  }

  const calls = fetchRecorder(async (url) => {
    if (url.startsWith("/api/onboarding")) return jsonResponse({ first_run_complete: false });
    if (url.startsWith("/api/media")) return jsonResponse({ ok: true, items: [] });
    if (url.startsWith("/api/connect/")) return jsonResponse({ ok: true, connected: false });
    if (url === "/api/post") return jsonResponse(DRY_RUN_OK);
    throw new Error(`unexpected ${url}`);
  });

  await api.onboarding();
  await api.saveOnboarding({ local: { path: "/videos" }, destinations: { youtube: true } });
  await api.completeOnboarding();
  await api.media("/videos");
  await api.connect("youtube", { dry_run: true });
  await api.post({ media_paths: ["a.mp4"], caption: "c", platforms: ["youtube"], dry_run: true });

  assert.deepEqual(calls.map((call) => call.url), [
    "/api/onboarding",
    "/api/onboarding",
    "/api/onboarding/complete",
    "/api/media?folder=%2Fvideos",
    "/api/connect/youtube",
    "/api/post",
  ]);
  assert.deepEqual(calls.map((call) => call.options.method ?? "GET"), ["GET", "POST", "POST", "GET", "POST", "POST"]);
  assert.deepEqual(JSON.parse(calls[1].options.body), { local: { path: "/videos" }, destinations: { youtube: true } });
  assert.deepEqual(JSON.parse(calls[5].options.body), {
    media_paths: ["a.mp4"],
    caption: "c",
    platforms: ["youtube"],
    dry_run: true,
  });
});

test("a refused post surfaces the engine's own status and body", async () => {
  fetchRecorder(async () => jsonResponse({ ok: false, blockers: ["Choose at least one destination platform."], destinations: [] }, 409));
  await assert.rejects(
    () => api.post({ media_paths: ["a.mp4"], caption: "c", platforms: [] }),
    (error) => {
      assert.ok(error instanceof ApiError);
      assert.equal(error.status, 409);
      assert.deepEqual(error.body.blockers, ["Choose at least one destination platform."]);
      // The body is what the result screen renders — a 409 must never look like a success.
      assert.equal(error.body.ok, false);
      return true;
    }
  );
});

test("an unreachable engine and a non-JSON reply are explicit errors", async () => {
  globalThis.fetch = async () => {
    throw new TypeError("fetch failed");
  };
  await assert.rejects(() => api.onboarding(), /engine unreachable/);

  fetchRecorder(async () => new Response("<html>nope</html>", { status: 200, headers: { "content-type": "text/html" } }));
  await assert.rejects(() => api.onboarding(), /not JSON/);
});

// ── Session store ────────────────────────────────────────────────────────

test("the last post is remembered, published to subscribers, and clearable", () => {
  clearLastPost();
  assert.deepEqual(getLastPost(), emptyLastPost());

  const seen = [];
  const unsubscribe = subscribeLastPost((value) => seen.push(value));
  setLastPost({ result: REAL_SUCCESS, request: { platforms: ["youtube"] } });
  assert.equal(getLastPost().result.ok, true);
  assert.equal(seen.length, 1);
  unsubscribe();
  clearLastPost();
  assert.equal(seen.length, 1, "an unsubscribed listener must not be called");
  assert.equal(getLastPost().result, null);
});

// ── Screens ──────────────────────────────────────────────────────────────

test("every flow screen has explicit empty and error states", async () => {
  for (const page of ["Onboarding", "Connect", "Compose", "Result"]) {
    const source = await text(join(UI_ROOT, "src/pages", `${page}.svelte`));
    assert.match(source, /ErrorState/, `${page} has no error state`);
    assert.match(source, /EmptyState/, `${page} has no empty state`);
    assert.match(source, /<h1>/, `${page} has no heading`);
  }
});

test("every flow screen that waits on the engine has a loading state", async () => {
  // Onboarding, Connect and Compose fetch from the engine on mount, so they
  // must show a skeleton while that request is outstanding. Result has no
  // loading phase on purpose: it renders the record this session already
  // holds, and a spinner over local data would be invented work.
  for (const page of ["Onboarding", "Connect", "Compose"]) {
    const source = await text(join(UI_ROOT, "src/pages", `${page}.svelte`));
    assert.match(source, /LoadingSkeleton/, `${page} has no loading state`);
    assert.match(source, /=== "loading"/, `${page} does not gate on a loading state`);
  }
  const result = await text(join(UI_ROOT, "src/pages/Result.svelte"));
  assert.doesNotMatch(result, /LoadingSkeleton/, "Result must not show a skeleton for a synchronous read");
});

test("the compose screen posts through the engine and defers to its verdict", async () => {
  const compose = await text(join(UI_ROOT, "src/pages/Compose.svelte"));
  assert.match(compose, /api\.post\(/);
  assert.match(compose, /dry_run/);
  assert.match(compose, /setLastPost/);
  // A refusal is recorded as a result, never swallowed.
  assert.match(compose, /setLastPost\(\{ result: body, request, error: null \}\)/);
});

test("the result screen states failures explicitly and offers a retry path", async () => {
  const result = await text(join(UI_ROOT, "src/pages/Result.svelte"));
  assert.match(result, /resultHeadline/);
  assert.match(result, /destinationOutcomes/);
  assert.match(result, /never marks a failed upload as complete/);
  assert.match(result, /record\?\.error/);
  assert.match(result, /href="#\/compose"/);
});

test("every flow screen compiles with the Svelte compiler", async () => {
  for (const page of ["Onboarding", "Connect", "Compose", "Result"]) {
    const source = await text(join(UI_ROOT, "src/pages", `${page}.svelte`));
    const { js, warnings } = compile(source, { filename: `${page}.svelte`, generate: "client" });
    assert.ok(js.code.length > 0, `${page} compiled to nothing`);
    const fatal = (warnings ?? []).filter((warning) => warning.code === "missing-declaration");
    assert.deepEqual(fatal, [], `${page} has undeclared references: ${JSON.stringify(fatal)}`);
  }
});

test("the shell declares a favicon so the engine never 404s on every page load", async () => {
  const html = await text(join(UI_ROOT, "index.html"));
  assert.match(html, /rel="icon"/, "index.html does not declare an icon file");
  assert.ok(await exists(join(UI_ROOT, "public/favicon.png")), "ui/public/favicon.png is missing");
});
