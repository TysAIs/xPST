# UI asset and dependency manifest

This manifest records the source and license for every bundled design-system
asset or UI dependency. The web bundle must remain usable without a network
connection and must not add telemetry.

| Item | Kind | Source | License | Usage |
| --- | --- | --- | --- | --- |
| `@lucide/svelte` `1.44.0` (the maintained successor to `lucide-svelte`) | npm dependency | <https://github.com/lucide-icons/lucide/tree/main/packages/lucide-svelte> | ISC (Feather-derived icons retain MIT notice) | Single icon family for navigation, status, and platform affordances. Bundled by Vite; no icon CDN. |
| `@fontsource-variable/inter` | npm dependency | <https://github.com/fontsource/fontsource/tree/main/fonts/inter> | SIL Open Font License 1.1 | Self-hosted UI typeface; imported from the package in `app.css`. |
| `assets/icon.png` | project asset | `../assets/icon.png` in this repository | xPST project license (`../LICENSE`) | Canonical icy-X web mark. `BrandMark.svelte` imports this exact local source so Vite fingerprints it into the build. |
| `assets/fonts/LICENSE-lucide.txt` | notice | `../assets/fonts/LICENSE-lucide.txt` in this repository | ISC + MIT notices | Existing repository notice retained for the desktop Lucide font; not loaded by the web UI. |
| `DESIGN.md` | design specification | <https://github.com/google-labs-code/design.md> | Apache License 2.0 (spec); project-authored values | Root design contract for tokens, components, and accessibility guidance. |

## Distribution notes

- `package-lock.json` pins the resolved npm graph. Update it together with
  `package.json`; do not replace these dependencies with remote `<script>` or
  stylesheet tags.
- The web mark is intentionally imported from `assets/icon.png`, not from the
  stale `assets/xpst-full.png` or the Tauri placeholder icons.
- npm package notices remain discoverable through their package metadata. This
  file is the UI-facing source/license index and does not replace upstream
  license text.
