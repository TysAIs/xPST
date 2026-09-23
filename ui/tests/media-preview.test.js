// Composer media-preview tests.
//
// The acceptance criteria these protect:
//   1. selecting an asset produces a real preview element (image thumbnail /
//      playable video), and
//   2. the preview path never reads the whole file — the preview is always a
//      URL for the engine's range-aware stream/thumbnail routes, never bytes
//      pulled into the page.
//
// The tests run against the real modules (media.js, native.js) plus a source
// check of the components, so a regression that "previews" by slurping the
// file into the webview fails here.

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { test } from "node:test";
import { compile } from "svelte/compiler";

import {
  formatDuration,
  isPreviewable,
  mediaItemsFromPaths,
  mediaKind,
  mediaStreamUrl,
  mediaThumbUrl,
  mergeMedia,
  pathOf,
  previewCaption,
  previewPlan,
  unsupportedPaths,
} from "../src/lib/media.js";
import { installShellDropTarget, pickMediaFile, shellAvailable } from "../src/lib/native.js";

const UI_ROOT = resolve(import.meta.dirname, "..");

async function text(path) {
  return readFile(path, "utf8");
}

const VIDEO = { path: "/media/clip.mp4", name: "clip.mp4", type: "video", size_bytes: 12_000_000 };
const IMAGE = { path: "/media/pic.png", name: "pic.png", type: "image", size_bytes: 800_000 };

// ── Preview plan ─────────────────────────────────────────────────────────

test("a selected asset produces a preview plan pointing at the engine, not the bytes", () => {
  const video = previewPlan(VIDEO);
  assert.equal(video.kind, "video");
  assert.equal(video.src, "/api/media/stream?path=%2Fmedia%2Fclip.mp4");
  assert.equal(video.poster, "/api/media/thumb?path=%2Fmedia%2Fclip.mp4&width=640");
  assert.equal(video.controls, true, "a video preview must be playable, not a still");
  assert.equal(video.preload, "metadata", "preload=auto would buffer the whole file");

  const image = previewPlan(IMAGE);
  assert.equal(image.kind, "image");
  assert.equal(image.src, "/api/media/thumb?path=%2Fmedia%2Fpic.png&width=640");
  assert.equal(image.fallbackSrc, "/api/media/stream?path=%2Fmedia%2Fpic.png");

  assert.equal(previewPlan({ path: "/media/notes.txt", type: "video" }), null);
  assert.equal(previewPlan(null), null);
  assert.equal(previewPlan({ path: "" }), null);
});

test("preview URLs stay on the same origin and encode the path exactly once", () => {
  const weird = "/Users/me/My Videos/take #1 & 2.mp4";
  const url = mediaStreamUrl(weird);
  assert.ok(url.startsWith("/api/media/stream?path="), url);
  assert.match(url, /%2FUsers%2Fme%2FMy%20Videos%2Ftake%20%231%20%26%202\.mp4/);
  assert.equal(mediaStreamUrl(weird).split("path=").length, 2, "the path must appear exactly once");
  assert.match(mediaThumbUrl(weird, 320), /width=320$/);
});

test("the preview path never reads media bytes in the page", async () => {
  // The forbidden implementations: a FileReader, an arrayBuffer/Buffer read of
  // a dropped file, or a fetch of the file's bytes into the UI.
  const media = await text(join(UI_ROOT, "src/lib/media.js"));
  const native = await text(join(UI_ROOT, "src/lib/native.js"));
  const preview = await text(join(UI_ROOT, "src/lib/components/MediaPreview.svelte"));
  const compose = await text(join(UI_ROOT, "src/pages/Compose.svelte"));

  for (const [name, source] of [
    ["media.js", media],
    ["native.js", native],
    ["MediaPreview.svelte", preview],
    ["Compose.svelte", compose],
  ]) {
    assert.doesNotMatch(source, /FileReader|readAsArrayBuffer|readAsDataURL|arrayBuffer\(/, `${name} reads file bytes`);
    assert.doesNotMatch(source, /createObjectURL|blob:/, `${name} copies the file into a blob`);
  }
  // The one way media reaches the preview surface is the engine's stream route.
  assert.match(preview, /previewPlan\(/);
  assert.match(preview, /plan\.src/);
});

test("the preview component renders a video element for video and an img for images", async () => {
  const preview = await text(join(UI_ROOT, "src/lib/components/MediaPreview.svelte"));
  assert.match(preview, /<video/);
  assert.match(preview, /controls/, "the video preview must be playable");
  assert.match(preview, /preload=\{plan\.preload\}/);
  assert.match(preview, /<img/);
  assert.match(preview, /onerror=/, "an image whose thumbnail 404s must fall back to the stream");
  assert.match(preview, /data-testid="media-preview"/);
});

// ── Item plumbing ────────────────────────────────────────────────────────

test("paths from the native picker/drop become previewable items", () => {
  const items = mediaItemsFromPaths(["/a/clip.mov", "/a/pic.JPG", "/a/notes.txt", ""]);
  assert.deepEqual(
    items.map((item) => [item.name, item.type]),
    [
      ["clip.mov", "video"],
      ["pic.JPG", "image"],
    ]
  );
  assert.deepEqual(unsupportedPaths(["/a/clip.mov", "/a/notes.txt"]), ["/a/notes.txt"]);
  assert.equal(isPreviewable("/a/pic.webp"), true);
  assert.equal(mediaKind({ path: "x", type: "IMAGE" }), "image");
  assert.equal(mediaKind({ path: "x.unknown" }), "unknown");
});

test("mergeMedia de-duplicates by path with the newest additions first", () => {
  const existing = [VIDEO, IMAGE];
  const merged = mergeMedia(existing, [{ path: "/media/new.mp4", name: "new.mp4", type: "video" }, IMAGE]);
  assert.deepEqual(merged.map((item) => item.path), ["/media/new.mp4", "/media/pic.png", "/media/clip.mp4"]);
  assert.equal(mergeMedia([], ["/media/plain.mp4"])[0].name, "plain.mp4");
  assert.equal(pathOf(null), "");
});

test("captions describe the asset without inventing metadata", () => {
  assert.equal(previewCaption(VIDEO), "Video · 11 MB");
  assert.equal(previewCaption(VIDEO, { duration: 65.4, dimensions: "1080×1920" }), "Video · 11 MB · 1:05 · 1080×1920");
  assert.equal(previewCaption({ path: "/x/a.mp4", type: "video" }), "Video");
  assert.equal(previewCaption({ path: "/x/a.txt" }), "Not previewable");
  assert.equal(formatDuration(null), "");
  assert.equal(formatDuration(9.9), "0:09");
});

// ── Shell bridge ─────────────────────────────────────────────────────────

function withFakeShell(invoke) {
  const previous = globalThis.__TAURI_INTERNALS__;
  globalThis.__TAURI_INTERNALS__ = { invoke };
  return () => {
    if (previous === undefined) delete globalThis.__TAURI_INTERNALS__;
    else globalThis.__TAURI_INTERNALS__ = previous;
  };
}

test("outside the app window the picker reports unavailable instead of a path", async () => {
  const restore = withFakeShell(undefined);
  delete globalThis.__TAURI_INTERNALS__;
  try {
    assert.equal(shellAvailable(), false);
    const result = await pickMediaFile();
    assert.equal(result.ok, false);
    assert.equal(result.reason, "unavailable");
    assert.equal(result.path, undefined, "a browser must never be handed a fabricated path");
  } finally {
    restore();
  }
});

test("in the app window the picker returns the shell's path and filters to media", async () => {
  const calls = [];
  const restore = withFakeShell(async (command, args) => {
    calls.push({ command, args });
    return "/Users/me/video.mp4";
  });
  try {
    assert.equal(shellAvailable(), true);
    const result = await pickMediaFile();
    assert.deepEqual(result, { ok: true, path: "/Users/me/video.mp4" });
    assert.equal(calls[0].command, "plugin:dialog|open");
    assert.equal(calls[0].args.options.directory, false);
    assert.ok(calls[0].args.options.filters[0].extensions.includes("mp4"));
    assert.ok(!calls[0].args.options.filters[0].extensions.some((extension) => extension.startsWith(".")));
  } finally {
    restore();
  }
});

test("a cancelled picker and a refused IPC call are reported honestly", async () => {
  let restore = withFakeShell(async () => null);
  try {
    assert.deepEqual(await pickMediaFile(), { ok: false, reason: "cancelled" });
  } finally {
    restore();
  }

  restore = withFakeShell(async () => {
    throw new Error("dialog.open not allowed. Permissions associated with this command: dialog:allow-open");
  });
  try {
    const result = await pickMediaFile();
    assert.equal(result.ok, false);
    assert.equal(result.reason, "denied");
    assert.match(result.error, /dialog:allow-open/, "the shell's own refusal must be surfaced verbatim");
  } finally {
    restore();
  }
});

test("the shell drop target is registered as the hook the Rust side calls", () => {
  const seen = [];
  const uninstall = installShellDropTarget((phase, paths) => seen.push([phase, paths]));
  assert.equal(typeof globalThis.__xpstMediaDrag, "function");
  globalThis.__xpstMediaDrag("drop", ["/Users/me/dropped.mp4"]);
  assert.deepEqual(seen, [["drop", ["/Users/me/dropped.mp4"]]]);
  // A handler that throws must not break the drop.
  installShellDropTarget(() => {
    throw new Error("boom");
  });
  globalThis.__xpstMediaDrag("leave", []);
  uninstall();
  assert.equal(globalThis.__xpstMediaDrag, undefined);
});

// ── Compose wiring ───────────────────────────────────────────────────────

test("the composer previews the selection, and wires picker + drag-drop to it", async () => {
  const compose = await text(join(UI_ROOT, "src/pages/Compose.svelte"));
  assert.match(compose, /<MediaPreview item=\{selectedItem\}/);
  assert.match(compose, /installShellDropTarget\(/);
  assert.match(compose, /pickMediaFile\(/);
  assert.match(compose, /data-drop-active=/);
  assert.match(compose, /ondrop=\{onDomDrop\}/);
  // Dropped/picked media joins the list and becomes the selection.
  assert.match(compose, /dropped = mergeMedia\(dropped, accepted\)/);
  assert.match(compose, /selectedMedia = accepted\[0\]\.path/);
});

test("the composer previews and the media card compile", async () => {
  for (const file of ["src/lib/components/MediaPreview.svelte", "src/pages/Compose.svelte"]) {
    const source = await text(join(UI_ROOT, file));
    const { js, warnings } = compile(source, { filename: file, generate: "client" });
    assert.ok(js.code.length > 0, `${file} compiled to nothing`);
    const fatal = (warnings ?? []).filter((warning) => warning.code === "missing-declaration");
    assert.deepEqual(fatal, [], `${file} has undeclared references: ${JSON.stringify(fatal)}`);
  }
});

test("the composer rejects a drag-drop it cannot honestly serve, in a browser", async () => {
  const compose = await text(join(UI_ROOT, "src/pages/Compose.svelte"));
  assert.match(compose, /a browser cannot hand xPST a filesystem path/);
  assert.match(compose, /only available in the xPST app window/);
});
