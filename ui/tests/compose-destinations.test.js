// The compose screen must not start a post with no destination selected.
//
// The shipped defect this guards: the post button was enabled whenever a video
// was chosen, so it read "Post to 0 destinations" and started a run that could
// publish nothing (and, before the engine reported outcomes honestly, could
// read as a success). The reason the button is disabled is the same sentence the
// CLI, the MCP server and the HTTP API refuse with, so the UI explains the rule
// instead of restating it differently.

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { test } from "node:test";

import {
  NO_DESTINATIONS_REASON,
  composePostLabel,
  composePostState,
} from "../src/lib/firstRun.js";

const UI_ROOT = resolve(import.meta.dirname, "..");

test("a video with no destination selected cannot be posted", () => {
  const state = composePostState({ mediaPath: "/videos/clip.mp4", chosen: [] });
  assert.equal(state.canPost, false);
  assert.equal(state.reason, NO_DESTINATIONS_REASON);
  // Pinned wording — the same string the engine, the CLI and MCP return.
  assert.equal(state.reason, "Choose at least one destination platform.");
  assert.equal(state.count, 0);
});

test("a video with a destination selected can be posted", () => {
  const state = composePostState({
    mediaPath: "/videos/clip.mp4",
    chosen: [{ name: "youtube", ready: true }],
  });
  assert.equal(state.canPost, true);
  assert.equal(state.reason, "");
  assert.equal(state.count, 1);
});

test("the missing video is reported before the missing destination", () => {
  const state = composePostState({ mediaPath: "", chosen: [] });
  assert.equal(state.canPost, false);
  assert.equal(state.reason, "Choose a video before posting.");
});

test("an in-flight post disables the control without a reason banner", () => {
  const state = composePostState({
    mediaPath: "/videos/clip.mp4",
    chosen: [{ name: "youtube", ready: true }],
    busy: true,
  });
  assert.equal(state.canPost, false);
  assert.equal(state.reason, "");
});

test("the button label never claims a destination count it does not have", () => {
  assert.equal(composePostLabel({ count: 0 }), "Post");
  assert.equal(composePostLabel({ dryRun: true, count: 0 }), "Run dry run");
  assert.equal(composePostLabel({ count: 1 }), "Post to 1 destination");
  assert.equal(composePostLabel({ count: 3 }), "Post to 3 destinations");
  assert.doesNotMatch(composePostLabel({ count: 0 }), /0 destination/);
});

test("the compose screen wires the disabled control to a visible reason", async () => {
  const source = await readFile(join(UI_ROOT, "src/pages/Compose.svelte"), "utf8");
  assert.match(source, /composePostState/, "Compose does not use the shared post state");
  assert.match(source, /disabled=\{!canPost\}/, "the post control is not disabled by the guard");
  assert.match(source, /id="compose-post-blocked-reason"/, "no visible reason element");
  assert.match(source, /aria-describedby=\{postState\.reason/, "the reason is not exposed to assistive tech");
  assert.match(source, /Posting is disabled: \{postState\.reason\}/, "the reason is not rendered");
});
