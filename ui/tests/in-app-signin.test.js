import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { test } from "node:test";
import { api } from "../src/lib/api.js";

// The in-app Sign in control (B1) has two halves that must stay in sync: the
// UI drives three endpoints, and it must never claim an account is connected
// before the engine's own live probe says so. These tests pin the client
// contract; the state machine itself is covered by the engine's pytest suite.

const UI_ROOT = resolve(import.meta.dirname, "..");

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

const WAITING = {
  session_id: "sess-1",
  platform: "youtube",
  phase: "waiting",
  terminal: false,
  authorize_url: "https://accounts.google.com/o/oauth2/auth?state=abc",
  redirect_uri: "http://127.0.0.1:8085/",
  transport: "loopback",
  browser: "Brave Browser",
  browser_opened: true,
  detail: "Waiting for you to approve xPST in Brave Browser.",
  error: "",
  error_code: "",
  error_description: "",
  poll_after_ms: 1000,
};

test("the client exposes the sign-in surface and uses the right verbs", async () => {
  for (const name of ["startSignIn", "signInStatus", "cancelSignIn", "signIns"]) {
    assert.equal(typeof api[name], "function", `api.${name} is missing`);
  }

  const calls = fetchRecorder(async (url) => {
    if (url === "/api/auth/signin/youtube") return jsonResponse(WAITING);
    if (url === "/api/auth/signin/sess-1") return jsonResponse({ ...WAITING, phase: "succeeded", terminal: true });
    if (url === "/api/auth/signin/sess-1/cancel") return jsonResponse({ ...WAITING, phase: "cancelled", terminal: true });
    if (url === "/api/auth/signin") return jsonResponse({ ok: true, sessions: [WAITING] });
    throw new Error(`unexpected ${url}`);
  });

  await api.startSignIn("youtube");
  await api.signInStatus("sess-1");
  await api.cancelSignIn("sess-1");
  await api.signIns();

  assert.deepEqual(calls.map((call) => call.url), [
    "/api/auth/signin/youtube",
    "/api/auth/signin/sess-1",
    "/api/auth/signin/sess-1/cancel",
    "/api/auth/signin",
  ]);
  assert.deepEqual(calls.map((call) => call.options.method ?? "GET"), ["POST", "GET", "POST", "GET"]);
});

test("a refused sign-in surfaces the engine's reason verbatim", async () => {
  fetchRecorder(async () =>
    jsonResponse({ detail: "Instagram needs a Meta developer app with this account added to it." }, 409)
  );
  await assert.rejects(
    () => api.startSignIn("instagram"),
    (error) => {
      assert.equal(error.status, 409);
      assert.match(error.detail, /Meta developer app/);
      return true;
    }
  );
});

test("the Connect page drives the state machine and re-verifies after approval", async () => {
  const page = await readFile(join(UI_ROOT, "src/pages/Connect.svelte"), "utf8");

  // It starts, polls and cancels through the engine (no terminal instructions).
  assert.match(page, /api\.startSignIn\(/);
  assert.match(page, /api\.signInStatus\(/);
  assert.match(page, /api\.cancelSignIn\(/);
  assert.match(page, /poll_after_ms/);

  // Waiting / cancel / failure are all rendered states.
  assert.match(page, /Waiting for you to approve/);
  assert.match(page, /Cancel sign-in/);
  assert.match(page, /signIn\.error/);

  // Approval re-verifies with the canonical probe before claiming success.
  assert.match(page, /await verify\(signIn\.platform\)/);
  assert.doesNotMatch(page, /Sign-in itself needs a browser or terminal/);

  // Unavailable platforms explain themselves instead of offering a dead button.
  assert.match(page, /signInSupport\?\.available/);

  // The control keys off the destination state, not `authenticated`: an
  // authenticated *source* session (TikTok cookies) must not hide it.
  assert.match(page, /needsSignIn/);
});
