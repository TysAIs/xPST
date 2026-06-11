# Owner Smoke Checklists — macOS + Windows (v0.1.0-rc)

> The owner runs these by hand on the Mac and Windows laptops (this dev box is
> Linux/aarch64 and cannot render the desktop app). Each line is a yes/no.
> Evidence: note pass/fail + a screenshot for anything visual. Results land
> in ISA.md `## Verification` (ISC-41, ISC-45, ISC-56, ISC-57).

## Build (per OS)

```
git pull && uv sync --all-extras
uv run pyinstaller build_macos.spec    # mac
uv run pyinstaller build_windows.spec  # windows
```

- [ ] Build completes without errors
- [ ] Binary launches from Finder/Explorer (not just terminal)
- [ ] macOS: note the Gatekeeper prompt wording (unsigned build expected)
- [ ] Windows: note the SmartScreen prompt wording (unsigned build expected)

## App open + UI polish (ISC-41, ISC-45)

- [ ] Splash → main window transition is smooth (no flash/stall)
- [ ] `--no-splash` flag actually skips the splash (this was broken before)
- [ ] Window size/position SURVIVES a quit + relaunch (new in this build)
- [ ] No tofu/□ glyphs anywhere: check Sidebar, Dashboard, Analytics tabs,
      Content page rows, Connect page provider cards, About links
- [ ] Sidebar nav highlight animates (150ms) on hover/selection
- [ ] Analytics platform tab color animates on switch
- [ ] Dark/light theme toggle: no hardcoded-color artifacts
- [ ] Drag-drop a video file onto the window — Windows especially
      (`file:///C:/...` paths were broken before): caption dialog appears
      with the correct file

## Functional smoke (ISC-56, ISC-57)

- [ ] `xpst setup` walks through cleanly
- [ ] `xpst connect youtube` (or reuse existing session) succeeds
- [ ] `xpst health` shows platforms + NEW "Stored Sessions" section with ages
- [ ] Post one real test video (private/unlisted where possible):
      `xpst post <file> --caption "rc smoke" --platforms youtube`
- [ ] Verify upload quality on the platform: a 1080x1920 source must arrive
      1080x1920 (not 608x1080) — THE headline fix; screenshot the quality
- [ ] Post the SAME file again — it must skip as already-posted (dedup)
- [ ] `xpst analytics --json` returns parseable JSON
- [ ] `xpst failures list` runs (empty is fine)
- [ ] `xpst state backup` creates ~/.xpst/backups/state-*.json
- [ ] Desktop Analytics page shows real numbers (or clean empty state),
      no fake "+28% vs last week" (should say "no history yet" if compared)
- [ ] `xpst kb query "anything" --json` runs (substring mode OK without extras)

## Sign-off

- OS + version: ____________
- Result: PASS / FAIL (list failures): ____________
- Screenshots attached: yes/no
