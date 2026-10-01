// Instagram account-type visibility (B2). Meta's API only publishes to
// Business/Creator accounts, so the Accounts page must state the verified
// type and never leave a personal account reading as healthy. These tests
// pin the page's copy and the honest fallback, and compile the screen so the
// markup cannot drift silently.

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { test } from "node:test";
import { compile } from "svelte/compiler";

const PAGE = resolve(import.meta.dirname, "../src/pages/Accounts.svelte");

test("the Accounts page compiles", async () => {
  const source = await readFile(PAGE, "utf8");
  compile(source, { filename: "Accounts.svelte" });
});

test("the page labels every verified account type and defaults to not-confirmed", async () => {
  const source = await readFile(PAGE, "utf8");
  assert.match(source, /business:\s*"Business account"/);
  assert.match(source, /creator:\s*"Creator account"/);
  assert.match(source, /personal:\s*"Personal account/);
  // An unprobed account must render an explicit "not yet confirmed" note —
  // silence or a green pill would hide the state the card must never hide.
  assert.match(source, /Account type not yet confirmed/);
  assert.match(source, /data-testid="ig-account-type"/);
});

test("the note only applies to Instagram", async () => {
  const source = await readFile(PAGE, "utf8");
  assert.match(source, /if \(provider\.name !== "instagram"\) return ""/);
});
