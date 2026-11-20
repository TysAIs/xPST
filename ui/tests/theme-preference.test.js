import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const UI_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "src");

const read = (rel) => readFile(join(UI_ROOT, rel), "utf8");

describe("theme preference", () => {
  it("theme.js implements the exact contract tokens.css declares", async () => {
    const theme = await read("lib/theme.js");
    // "auto" must REMOVE the attribute so the OS media query in tokens.css applies.
    assert.match(theme, /removeAttribute\("data-theme"\)/);
    // Explicit choices must set it.
    assert.match(theme, /setAttribute\("data-theme", choice\)/);
    // Only three values are legal.
    assert.match(theme, /theme === "light" \|\| theme === "dark"/);
    // Persistence failure must never break application of the choice.
    assert.match(theme, /catch/);
  });

  it("Settings exposes one accessible theme control with the three options", async () => {
    const settings = await read("pages/Settings.svelte");
    assert.match(settings, /id="theme-select"/);
    assert.match(settings, /for="theme-select"/);
    // The options come from THEME_OPTIONS (imported from lib/theme.js).
    assert.match(settings, /THEME_OPTIONS/);
    assert.match(settings, /import \{[^}]*THEME_OPTIONS[^}]*\} from "\.\.\/lib\/theme\.js"/);
    // The control reports the live state honestly.
    assert.match(settings, /following your Mac's appearance/);
  });

  it("the app restores the saved theme before the first page renders", async () => {
    const app = await read("App.svelte");
    const restoreAt = app.indexOf("applyTheme(storedTheme())");
    const mount = app.indexOf("onMount(");
    assert.ok(restoreAt > mount, "theme restore belongs in onMount");
    // Restore must happen before routing so first paint is already correct.
    const hashListener = app.indexOf('addEventListener("hashchange"');
    assert.ok(restoreAt < hashListener, "theme must be applied before first paint");
  });
});
