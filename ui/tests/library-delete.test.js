import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const src = (p) => readFile(join(here, "..", "src", p), "utf8");

describe("library delete feature", () => {
  it("Library offers delete with a two-step confirm, never one-click", async () => {
    const page = await src("pages/Library.svelte");
    assert.match(page, /Delete post…/);
    assert.match(page, /Yes, delete/);
    assert.match(page, /Keep it/);
    assert.match(page, /This cannot be undone/);
  });

  it("delete goes through the engine contract route with honest outcome handling", async () => {
    const api = await src("lib/api.js");
    assert.match(api, /\/api\/posts\/\$\{encodeURIComponent\(videoId\)\}\/delete/);
    const page = await src("pages/Library.svelte");
    // success requires the platform to confirm; refusals surface the message
    assert.match(page, /o === "deleted" \|\| o === "soft_hidden"/);
    assert.match(page, /deleteError = refused\?\.message/);
  });

  it("danger variant exists in the design system for destructive actions", async () => {
    const css = await src("app.css");
    assert.match(css, /\.xpst-button\[data-variant="danger"\]/);
  });
});
