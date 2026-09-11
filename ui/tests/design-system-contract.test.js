import assert from "node:assert/strict";
import { readFile, access } from "node:fs/promises";
import { join, resolve } from "node:path";
import { test } from "node:test";
import { currentRoute, NAV_ITEMS } from "../src/lib/api.js";
import { platformLabel, statusLabel } from "../src/lib/labels.js";

const UI_ROOT = resolve(import.meta.dirname, "..");
const REPO_ROOT = resolve(UI_ROOT, "..");

async function text(path) {
  return readFile(path, "utf8");
}

async function exists(path) {
  try {
    await access(path);
    return true;
  } catch {
    return false;
  }
}

test("DESIGN.md declares the canonical token sections in order", async () => {
  const design = await text(join(REPO_ROOT, "DESIGN.md"));
  assert.match(design, /^---\nversion:/);
  assert.match(design, /\nname: xPST Design System\n/);
  assert.match(design, /\ncolors:\n/);
  assert.match(design, /\ntypography:\n/);
  assert.match(design, /\nspacing:\n/);
  assert.match(design, /\nmotion:/);
  assert.match(design, /\nfocus:/);
  const headings = [...design.matchAll(/^## (.+)$/gm)].map((match) => match[1]);
  assert.deepEqual(headings, [
    "Overview",
    "Colors",
    "Typography",
    "Layout",
    "Elevation & Depth",
    "Shapes",
    "Components",
    "Do's and Don'ts",
  ]);
});

test("the UI has local semantic tokens for both color modes and interaction states", async () => {
  const tokens = await text(join(UI_ROOT, "src/tokens.css"));
  for (const token of [
    "--xpst-color-background",
    "--xpst-color-surface",
    "--xpst-color-text-primary",
    "--xpst-space-4",
    "--xpst-type-body",
    "--xpst-radius-md",
    "--xpst-elevation-card",
    "--xpst-motion-standard",
    "--xpst-focus-ring",
  ]) {
    assert.match(tokens, new RegExp(token.replaceAll("-", "\\-")));
  }
  assert.match(tokens, /\[data-theme="dark"\]/);
  assert.match(tokens, /prefers-color-scheme: dark/);
  assert.match(tokens, /prefers-reduced-motion: reduce/);
});

test("all design-system primitives exist and declare accessible contracts", async () => {
  const components = [
    "Button",
    "Card",
    "StatusBadge",
    "PlatformBadge",
    "PlatformIcon",
    "EmptyState",
    "LoadingSkeleton",
    "ErrorState",
    "FormField",
    "Shell",
    "Nav",
    "BrandMark",
  ];
  for (const component of components) {
    const path = join(UI_ROOT, "src/lib/components", `${component}.svelte`);
    assert.equal(await exists(path), true, `${component} is missing`);
  }
  assert.match(await text(join(UI_ROOT, "src/lib/components/Button.svelte")), /focus-visible/);
  assert.match(await text(join(UI_ROOT, "src/lib/components/FormField.svelte")), /aria-describedby/);
  assert.match(await text(join(UI_ROOT, "src/lib/components/LoadingSkeleton.svelte")), /aria-busy/);
  assert.match(await text(join(UI_ROOT, "src/lib/components/ErrorState.svelte")), /Retry/);
  assert.match(await text(join(UI_ROOT, "src/lib/components/BrandMark.svelte")), /assets\/icon\.png/);
});

test("navigation uses one local icon family and exposes all foundation routes", () => {
  assert.deepEqual(NAV_ITEMS.map((item) => item.id), [
    "dashboard",
    "analytics",
    "videos",
    "accounts",
    "settings",
  ]);
  for (const item of NAV_ITEMS) {
    assert.match(item.icon, /^[a-z-]+$/);
  }
});

test("hash routing normalizes root, known routes, and unknown routes", () => {
  assert.equal(currentRoute("#/"), "dashboard");
  assert.equal(currentRoute("#/analytics"), "analytics");
  assert.equal(currentRoute("#videos"), "videos");
  assert.equal(currentRoute("#/not-a-route"), "dashboard");
});

test("the shell listens to hash and history navigation", async () => {
  const app = await text(join(UI_ROOT, "src/App.svelte"));
  assert.match(app, /addEventListener\("hashchange"/);
  assert.match(app, /addEventListener\("popstate"/);
  assert.match(app, /removeEventListener\("hashchange"/);
  assert.match(app, /removeEventListener\("popstate"/);
});

test("platform and status labels are human-readable without changing API values", () => {
  assert.equal(platformLabel("youtube"), "YouTube");
  assert.equal(platformLabel("instagram"), "Instagram");
  assert.equal(platformLabel("x"), "X");
  assert.equal(platformLabel("tiktok"), "TikTok");
  assert.equal(statusLabel("healthy"), "Healthy");
  assert.equal(statusLabel("ok"), "OK");
  assert.equal(statusLabel("degraded"), "Degraded");
});

test("dashboard empty action is placed before health and copy does not promise duplicate data", async () => {
  const dashboard = await text(join(UI_ROOT, "src/pages/Dashboard.svelte"));
  const emptyPosition = dashboard.indexOf("No posts tracked yet");
  const healthPosition = dashboard.indexOf("Engine health");
  assert.ok(emptyPosition >= 0 && emptyPosition < healthPosition);
  assert.match(dashboard, /Tracked source posts/);
  assert.match(dashboard, /Platform post records/);
  assert.doesNotMatch(dashboard, /platform health will appear here/);
});

test("the icon dependency and license manifest are explicit and offline-safe", async () => {
  const packageJson = JSON.parse(await text(join(UI_ROOT, "package.json")));
  assert.ok(packageJson.dependencies["lucide-svelte"] || packageJson.dependencies["@lucide/svelte"]);
  const manifest = await text(join(UI_ROOT, "THIRD_PARTY_NOTICES.md"));
  assert.match(manifest, /lucide-svelte/i);
  assert.match(manifest, /ISC/i);
  assert.match(manifest, /@fontsource-variable\/inter/i);
  assert.match(manifest, /SIL Open Font License/i);
  assert.match(manifest, /assets\/icon\.png/);
  assert.doesNotMatch(manifest, /https:\/\/fonts\.googleapis\.com/);
});
