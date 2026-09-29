import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { test } from "node:test";
import { api } from "../src/lib/api.js";

// The bring-your-own-app setup screen has two rules these tests pin:
//   1. the client uses the right endpoints and verbs (status is a GET, a
//      store/clear is a POST carrying only what the user typed);
//   2. the page never renders a raw secret — the engine's reply is masked,
//      and the page must not invent an echo of its own.

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

const MASKED = {
  platform: "instagram",
  display_name: "Instagram Reels",
  configurable: true,
  configured: true,
  half_configured: false,
  app_id_masked: "…3456",
  source: "encrypted-store",
  id_label: "Meta App ID",
  secret_label: "Meta App Secret",
  redirect_uri: "http://localhost:8888/callback",
  scopes: ["instagram_basic", "instagram_content_publish"],
  create_url: "https://developers.facebook.com/apps",
  docs_url: "https://developers.facebook.com/docs",
  enables: "Publishing Reels to your own account.",
  sign_in_available: true,
  env_vars: ["XPST_INSTAGRAM_APP_ID", "XPST_INSTAGRAM_APP_SECRET"],
};

test("the BYO client uses GET for status and POST for store/clear", async () => {
  const calls = fetchRecorder(async (url, options) => {
    if (url === "/api/byo" && options.method === undefined) return jsonResponse({ platforms: { instagram: MASKED } });
    if (url === "/api/byo/instagram" && options.method === "POST") {
      const body = JSON.parse(options.body);
      assert.ok(body.app_id === "1234567890123456" || body.clear === true, "payload carries id or clear only");
      assert.ok(!("app_secret" in body) || body.clear === true || body.app_secret.length > 0);
      return jsonResponse(MASKED);
    }
    throw new Error(`unexpected ${options.method ?? "GET"} ${url}`);
  });

  await api.byoApp();
  await api.setByoApp("instagram", { app_id: "1234567890123456", app_secret: "s3cr3t" });
  await api.setByoApp("instagram", { clear: true });

  assert.deepEqual(
    calls.map((c) => `${c.options.method ?? "GET"} ${c.url}`),
    ["GET /api/byo", "POST /api/byo/instagram", "POST /api/byo/instagram"],
  );
});

test("the Connect page renders the BYO panel from masked catalog truth only", async () => {
  const source = await readFile(`${UI_ROOT}/src/pages/Connect.svelte`, "utf8");

  // The panel exists and binds to the catalog's byo_app block (server-masked),
  // not to any client-side copy of the secret.
  assert.match(source, /byo_app/, "Connect.svelte must read the byo_app catalog block");
  assert.match(source, /setByoApp\(/, "Connect.svelte must store through the api client");
  assert.match(source, /app_id_masked/, "the stored badge must show the masked id only");
  assert.match(source, /type="password"/, "the secret input must be a password field");

  // The secret is cleared from local form state after a save: a stale
  // in-memory copy is the only echo the client could leak.
  assert.match(source, /appId: "", appSecret: "", saving: false/, "form state must clear the secret after storing");
});
