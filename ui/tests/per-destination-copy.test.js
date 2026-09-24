// Per-destination copy: the composer must be able to send a different caption
// per destination, the request summary must carry it, and the result screen
// must show the copy that was actually sent.

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { test } from "node:test";
import { compile } from "svelte/compiler";

import { destinationOutcomes, postRequestSummary } from "../src/lib/firstRun.js";

const UI_ROOT = resolve(import.meta.dirname, "..");

test("postRequestSummary carries the per-destination copy", () => {
  const summary = postRequestSummary({
    media_paths: ["/media/clip.mp4"],
    caption: "shared caption",
    overrides: { x: { text: "x-only caption" } },
    captions: { youtube: "shared caption", x: "x-only caption" },
    platforms: ["youtube", "x"],
    dry_run: false,
  });

  assert.deepEqual(summary.overrides, { x: { text: "x-only caption" } });
  assert.deepEqual(summary.captions, { youtube: "shared caption", x: "x-only caption" });
  assert.equal(summary.caption, "shared caption");
});

test("postRequestSummary tolerates a request with no overrides", () => {
  const summary = postRequestSummary({ caption: "shared", platforms: ["youtube"] });
  assert.deepEqual(summary.overrides, {});
  assert.deepEqual(summary.captions, {});
});

test("destinationOutcomes reports the caption sent to each destination", () => {
  const rows = destinationOutcomes({
    destinations: [
      { platform: "youtube", attempted: true, success: true, post_url: "https://example.invalid/yt", caption: "shared caption" },
      { platform: "x", attempted: true, success: true, post_url: "https://example.invalid/x", caption: "x-only caption" },
    ],
  });

  assert.deepEqual(
    rows.map((row) => [row.platform, row.caption]),
    [
      ["youtube", "shared caption"],
      ["x", "x-only caption"],
    ],
  );
});

test("destinationOutcomes leaves the caption null when the engine did not report one", () => {
  const rows = destinationOutcomes({ destinations: [{ platform: "x", attempted: false, success: null }] });
  assert.equal(rows[0].caption, null);
});

test("the composer exposes a per-destination caption field for each chosen destination", async () => {
  const source = await readFile(resolve(UI_ROOT, "src/pages/Compose.svelte"), "utf8");

  assert.match(source, /compose-caption-\$\{row\.name\}/, "no per-destination field id");
  assert.match(source, /Caption for \{row\.displayName\}/, "no per-destination label");
  assert.match(source, /overrides: sentOverrides/, "the post request does not carry the overrides");
  assert.doesNotThrow(() => compile(source, { filename: "Compose.svelte" }));
});

test("the result screen shows the caption sent per destination", async () => {
  const source = await readFile(resolve(UI_ROOT, "src/pages/Result.svelte"), "utf8");

  assert.match(source, /Caption sent/);
  assert.doesNotThrow(() => compile(source, { filename: "Result.svelte" }));
});
