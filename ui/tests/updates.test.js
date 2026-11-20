// Update-check contract tests.
//
// The defect this protects: an update channel that never runs for users, or one
// that claims an update/installs one without the user asking. These tests pin
// the three states the module may report and the fact that nothing installs on
// import — installation is only ever the result of an explicit call.

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { afterEach, test } from "node:test";
import { compile } from "svelte/compiler";

import {
  checkForUpdates,
  describeUpdateResult,
  installUpdate,
  updateNotes,
  updatesAvailable,
} from "../src/lib/updates.js";

const UI_ROOT = resolve(import.meta.dirname, "..");

function installTransport(invoke) {
  globalThis.__TAURI_INTERNALS__ = invoke ? { invoke } : undefined;
}

afterEach(() => {
  delete globalThis.__TAURI_INTERNALS__;
});

test("no shell transport reports unavailable instead of faking a check", async () => {
  installTransport(null);
  assert.equal(updatesAvailable(), false);
  const result = await checkForUpdates();
  assert.equal(result.status, "unavailable");
  assert.match(describeUpdateResult(result), /desktop app/);
});

test("an announced release is reported as available with its version", async () => {
  installTransport(async (command) => {
    assert.equal(command, "updater_check");
    return {
      current_version: "1.2.1",
      version: "1.3.0",
      notes: "- fixes",
      date: "2026-11-14T00:00:00Z",
    };
  });
  const result = await checkForUpdates();
  assert.equal(result.status, "available");
  assert.equal(result.update.version, "1.3.0");
  assert.equal(updateNotes(result), "- fixes");
  assert.match(describeUpdateResult(result), /1\.3\.0 is available/);
});

test("a null answer from the shell means up to date, not unknown", async () => {
  installTransport(async () => null);
  const result = await checkForUpdates();
  assert.equal(result.status, "current");
  assert.match(describeUpdateResult(result), /up to date/);
});

test("a failed check is reported as an error and never as current", async () => {
  installTransport(async () => {
    throw new Error("network unreachable");
  });
  const result = await checkForUpdates();
  assert.equal(result.status, "error");
  assert.match(describeUpdateResult(result), /network unreachable/);
});

test("install never runs on import and surfaces a failure honestly", async () => {
  let calls = 0;
  installTransport(async (command) => {
    calls += 1;
    assert.equal(command, "updater_install");
    throw new Error("signature verification failed");
  });
  assert.equal(calls, 0);
  const result = await installUpdate();
  assert.equal(result.ok, false);
  assert.match(result.error, /signature verification failed/);
  assert.equal(calls, 1);
});

test("the About page offers a visible update control", async () => {
  const source = await readFile(resolve(UI_ROOT, "src/pages/About.svelte"), "utf8");
  assert.match(source, /Check for updates/);
  assert.match(source, /Download and install/);
  const { warnings } = compile(source, { filename: "About.svelte" });
  assert.deepEqual(warnings, []);
});

test("the app checks once at launch", async () => {
  const source = await readFile(resolve(UI_ROOT, "src/App.svelte"), "utf8");
  assert.match(source, /if \(updatesAvailable\(\)\) refreshUpdateState\(\);/);
});
