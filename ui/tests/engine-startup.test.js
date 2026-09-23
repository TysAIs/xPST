// Startup-race contract for the UI.
//
// Measured on the installed bundle: the shell shows the window at
// BOOT_TO_VISIBLE_SECS=0.165 but the engine only answers at
// ENGINE_HEALTH_WAIT_SECS=0.690, so the first /api call of a launch fails.
// The old behaviour rendered that as a red card reading
// "Could not load this view / /api/summary → response was not JSON" — raw
// internals as the first thing the user ever saw.
//
// What must stay true:
//   1. "not JSON / unreachable" is a *starting* state, not a failure;
//   2. the starting state retries on its own and renders Home when the engine
//      answers, without the user pressing anything;
//   3. no user-facing message ever contains a route path or a raw internal
//      string, while the engine's own refusal reasons still reach the user;
//   4. a genuine failure still ends on the error card, bounded — never an
//      infinite spinner.

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { test } from "node:test";

import { ApiError, api, errorMessage, isEngineStarting } from "../src/lib/api.js";
import {
  BOOT_RETRY_ATTEMPTS,
  ENGINE_STARTING_TITLE,
  bootFailureMessage,
  bootRetryDelayMs,
  isBootTransient,
  loadWhileEngineStarts,
} from "../src/lib/engineStartup.js";

const UI_ROOT = resolve(import.meta.dirname, "..");
const PAGES = ["Dashboard", "Onboarding"];
const ALL_PAGES = [
  "Dashboard",
  "Onboarding",
  "Accounts",
  "Activity",
  "Analytics",
  "Compose",
  "Connect",
  "Create",
  "Library",
  "Result",
  "Schedule",
  "Settings",
  "Videos",
];

async function source(name) {
  return readFile(join(UI_ROOT, "src", "pages", `${name}.svelte`), "utf8");
}

/** Run `call` with a stubbed fetch; returns `{calls, result|error}`. */
async function withFetch(responder, call) {
  const calls = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (path, options = {}) => {
    calls.push({ path, options });
    return responder(path, options);
  };
  try {
    const value = await call();
    return { calls, value, error: undefined };
  } catch (error) {
    return { calls, value: undefined, error };
  } finally {
    globalThis.fetch = original;
  }
}

const htmlResponse = (status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  headers: { get: () => "text/html; charset=utf-8" },
  json: async () => {
    throw new Error("not json");
  },
});

const jsonResponse = (body, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  headers: { get: () => "application/json" },
  json: async () => body,
});

// ── 1. Classification ────────────────────────────────────────────────────

test("a non-JSON 200 is the engine not answering yet, not a broken view", async () => {
  const { error } = await withFetch(() => htmlResponse(), () => api.summary());

  assert.ok(error instanceof ApiError);
  assert.equal(error.kind, "engine-starting");
  assert.equal(isEngineStarting(error), true);
  assert.equal(isBootTransient(error), true);
  // The technical string is preserved for logs...
  assert.equal(error.message, "/api/summary → response was not JSON");
  // ...and never reaches the user.
  assert.doesNotMatch(errorMessage(error), /\/api\//);
  assert.doesNotMatch(errorMessage(error), /not JSON/);
});

test("an unreachable fetch is the engine not answering yet", async () => {
  const { error } = await withFetch(
    () => {
      throw new TypeError("Failed to fetch");
    },
    () => api.summary()
  );

  assert.equal(isEngineStarting(error), true);
  assert.equal(isBootTransient(error), true);
  assert.doesNotMatch(errorMessage(error), /\/api\//);
});

test("an engine refusal is not a starting state, and keeps its own reason", async () => {
  const { error } = await withFetch(
    () => jsonResponse({ detail: "no video file found in that folder" }, 409),
    () => api.post({ dry_run: true })
  );

  assert.equal(isEngineStarting(error), false);
  assert.equal(isBootTransient(error), false);
  assert.equal(errorMessage(error), "no video file found in that folder");
});

test("a refusal without an engine reason gets human copy, never a path", async () => {
  const { error } = await withFetch(() => jsonResponse({}, 404), () => api.videos());

  assert.equal(isBootTransient(error), false);
  const message = errorMessage(error);
  assert.doesNotMatch(message, /\/api\//);
  assert.doesNotMatch(message, /HTTP 404/);
  assert.match(message, /engine/i);
});

test("a 401 explains the token instead of echoing the route", async () => {
  const { error } = await withFetch(() => jsonResponse({}, 401), () => api.completeOnboarding());

  const message = errorMessage(error);
  assert.doesNotMatch(message, /\/api\//);
  assert.match(message, /token/i);
});

test("a 5xx during the boot window is transient, then reported honestly", async () => {
  const { error } = await withFetch(() => jsonResponse({}, 500), () => api.summary());

  assert.equal(isBootTransient(error), true, "a 500 while starting is worth a retry");
  assert.equal(isEngineStarting(error), false, "but it is not the plain not-answering case");
  assert.doesNotMatch(errorMessage(error), /\/api\//);
  assert.match(errorMessage(error), /restart xPST/i);
});

test("a non-ApiError still surfaces its own message", () => {
  assert.equal(errorMessage(new Error("boom")), "boom");
  assert.equal(errorMessage("boom"), "boom");
});

// ── 2. Bounded auto-retry ───────────────────────────────────────────────

test("Home recovers on its own once the engine answers", async () => {
  const waits = [];
  let attempt = 0;
  const result = await loadWhileEngineStarts(
    () => {
      attempt += 1;
      if (attempt < 3) return Promise.reject(new ApiError("/api/summary", 200, null, "raw", "engine-starting"));
      return Promise.resolve({ total_posts: 4 });
    },
    { sleep: async (ms) => waits.push(ms) }
  );

  assert.equal(result.ok, true);
  assert.deepEqual(result.value, { total_posts: 4 });
  assert.equal(result.attempts, 3);
  assert.deepEqual(waits, [200, 250], "short first steps, so a near-ready engine is not stalled");
});

test("the retry ladder grows and then caps", () => {
  assert.equal(bootRetryDelayMs(0), 200);
  assert.equal(bootRetryDelayMs(1), 200);
  assert.ok(bootRetryDelayMs(6) > bootRetryDelayMs(3));
  assert.equal(bootRetryDelayMs(99), bootRetryDelayMs(12), "capped so a dead engine still reports");
  assert.ok(bootRetryDelayMs(99) <= 2000);
});

test("a real failure is handed back immediately, with no retry loop", async () => {
  const waits = [];
  let calls = 0;
  const result = await loadWhileEngineStarts(
    () => {
      calls += 1;
      return Promise.reject(new ApiError("/api/onboarding", 409, { detail: "blocked" }, "raw"));
    },
    { sleep: async (ms) => waits.push(ms) }
  );

  assert.equal(result.ok, false);
  assert.equal(calls, 1, "a refusal is not retried");
  assert.deepEqual(waits, []);
  assert.equal(errorMessage(result.error), "blocked");
});

test("an engine that never answers ends in a real card, bounded", async () => {
  const waits = [];
  let calls = 0;
  const result = await loadWhileEngineStarts(
    () => {
      calls += 1;
      return Promise.reject(new ApiError("/api/summary", 200, null, "raw", "engine-starting"));
    },
    { attempts: 5, sleep: async (ms) => waits.push(ms) }
  );

  assert.equal(result.ok, false);
  assert.equal(result.engineStarting, true);
  assert.equal(calls, 5);
  assert.equal(waits.length, 4, "one wait between attempts, none after the last");
  assert.ok(
    waits.reduce((total, ms) => total + ms, 0) < 60_000,
    "the wait must not outlive the shell's own 60s engine timeout"
  );
  assert.ok(BOOT_RETRY_ATTEMPTS >= 5);
});

test("onWaiting fires only while the engine is still coming up", async () => {
  const seen = [];
  await loadWhileEngineStarts(
    () => Promise.reject(new ApiError("/api/summary", 200, null, "raw", "engine-starting")),
    { attempts: 3, sleep: async () => {}, onWaiting: (attempt) => seen.push(attempt) }
  );

  assert.deepEqual(seen, [1, 2, 3]);
});

test("running out of boot retries says so, instead of 'still connecting' forever", async () => {
  const result = await loadWhileEngineStarts(
    () => Promise.reject(new ApiError("/api/summary", 200, null, "raw", "engine-starting")),
    { attempts: 3, sleep: async () => {} }
  );

  const message = bootFailureMessage(result);
  assert.match(message, /did not answer/);
  assert.match(message, /Restart xPST/);
  assert.doesNotMatch(message, /\/api\//);
  // A refusal is not reworded by the boot path.
  assert.equal(
    bootFailureMessage({ engineStarting: false, error: new ApiError("/api/post", 409, { detail: "blocked" }, "raw") }),
    "blocked"
  );
});

// ── 3. Source contract ──────────────────────────────────────────────────

test("the landing views show the starting state before any error card", async () => {
  for (const name of PAGES) {
    const text = await source(name);
    assert.match(text, /loadWhileEngineStarts\(/, `${name} must survive the boot race`);
    assert.match(text, /EngineStarting/, `${name} must render the starting state`);
    assert.match(text, /bootFailureMessage\(/, `${name} must not print raw engine internals`);
    assert.ok(
      text.indexOf('{:else if state === "starting"}') < text.indexOf('{:else if state === "error"}'),
      `${name} must branch on "starting" before "error"`
    );
  }
});

test("no page puts a raw error message on screen", async () => {
  for (const name of ALL_PAGES) {
    const text = await source(name);
    assert.doesNotMatch(text, /cause\.message/, `${name} still renders the technical message`);
    assert.doesNotMatch(text, /response was not JSON/, `${name} still renders the raw internals`);
  }
});

test("the starting state is not an error card", async () => {
  const text = await readFile(
    join(UI_ROOT, "src", "lib", "components", "EngineStarting.svelte"),
    "utf8"
  );
  assert.match(text, /xpst-state--starting/);
  assert.match(text, /role="status"/);
  assert.doesNotMatch(text, /xpst-state--error/);
  assert.match(ENGINE_STARTING_TITLE, /^Starting/);
});

test("ErrorState keeps a human default for a genuine failure", async () => {
  const text = await readFile(
    join(UI_ROOT, "src", "lib", "components", "ErrorState.svelte"),
    "utf8"
  );
  assert.match(text, /title = "Could not load this view"/);
  assert.match(text, /Check the local engine and try again/);
});