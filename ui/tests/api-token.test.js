// The engine refuses mutating /api routes without the dashboard API token, so
// the UI client must attach it — and must never get it from the served HTML.

import assert from "node:assert/strict";
import { test } from "node:test";

import { api } from "../src/lib/api.js";
import {
  captureTokenFromFragment,
  getApiToken,
  parseTokenFragment,
  setApiToken,
  tokenHeaders,
} from "../src/lib/auth-token.js";

function fakeStorage(initial = {}) {
  const data = new Map(Object.entries(initial));
  return {
    getItem: (key) => (data.has(key) ? data.get(key) : null),
    setItem: (key, value) => data.set(key, String(value)),
    removeItem: (key) => data.delete(key),
    get size() {
      return data.size;
    },
  };
}

async function captureFetch(call) {
  const calls = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (path, options = {}) => {
    calls.push({ path, options });
    return {
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      json: async () => ({ ok: true }),
    };
  };
  try {
    await call();
  } finally {
    globalThis.fetch = original;
  }
  return calls;
}

test("mutating calls carry X-API-Token when a shell injected one", async (t) => {
  const original = globalThis.__XPST_API_TOKEN;
  globalThis.__XPST_API_TOKEN = "shell-token";
  t.after(() => {
    if (original === undefined) delete globalThis.__XPST_API_TOKEN;
    else globalThis.__XPST_API_TOKEN = original;
  });

  const calls = await captureFetch(() => api.post({ dry_run: true }));
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, "/api/post");
  assert.equal(calls[0].options.method, "POST");
  assert.equal(calls[0].options.headers["X-API-Token"], "shell-token");
});

test("no token means no header — callers get the engine's own 401 detail", async (t) => {
  const original = globalThis.__XPST_API_TOKEN;
  delete globalThis.__XPST_API_TOKEN;
  t.after(() => {
    if (original !== undefined) globalThis.__XPST_API_TOKEN = original;
  });

  const calls = await captureFetch(() => api.post({ dry_run: true }));
  assert.equal(calls[0].options.headers["X-API-Token"], undefined);
  assert.equal(calls[0].options.headers["Content-Type"], "application/json");
});

test("read-only calls stay unauthenticated so the UI is never locked out", async () => {
  const calls = await captureFetch(() => api.onboarding());
  assert.equal(calls[0].path, "/api/onboarding");
  assert.equal(calls[0].options.method, undefined);
});

test("the #xpst_token fragment is consumed and stripped from the URL", () => {
  const storage = fakeStorage();
  const replaced = [];
  const token = captureTokenFromFragment({
    locationObject: { hash: "#xpst_token=abc%2F123", pathname: "/", search: "" },
    historyObject: { replaceState: (...args) => replaced.push(args) },
    storage,
  });

  assert.equal(token, "abc/123");
  assert.equal(storage.getItem("xpst_api_token"), "abc/123");
  assert.equal(replaced.length, 1);
  assert.equal(replaced[0][2], "/#/");
});

test("a normal route hash leaves the token and the URL alone", () => {
  const storage = fakeStorage();
  const replaced = [];
  const token = captureTokenFromFragment({
    locationObject: { hash: "#/compose", pathname: "/", search: "" },
    historyObject: { replaceState: (...args) => replaced.push(args) },
    storage,
  });

  assert.equal(token, null);
  assert.equal(storage.size, 0);
  assert.equal(replaced.length, 0);
});

test("sessionStorage keeps the token for the rest of the tab session", () => {
  const storage = fakeStorage();
  setApiToken("stored-token", { storage });
  assert.equal(getApiToken({ storage }), "stored-token");
  assert.equal(tokenHeaders({ storage })["X-API-Token"], "stored-token");

  setApiToken("", { storage });
  assert.equal(getApiToken({ storage }), null);
});

test("an injected shell token wins over a stored one", () => {
  const storage = fakeStorage({ xpst_api_token: "stored-token" });
  assert.equal(parseTokenFragment("#xpst_token=zzz"), "zzz");
  assert.equal(
    getApiToken({ globalObject: { __XPST_API_TOKEN: "injected" }, storage }),
    "injected"
  );
});
