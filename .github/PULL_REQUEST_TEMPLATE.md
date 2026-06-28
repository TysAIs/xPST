# Summary

<!-- One or two sentences: what does this PR do and why? Link the issue if one exists. -->

## Changes

<!-- Bullet list of the concrete changes. Call out anything risky: state format, posting paths, auth, encoding. -->

-

## Verification Evidence

<!-- Claims need evidence. Paste command output or describe the manual check. -->

- [ ] Gates green: `pytest` / `ruff check src tests` / `mypy src/xpst` / `lint-imports` / `pip-audit`
- [ ] New or changed behavior is covered by tests (or the verification gap is stated below)
- [ ] No live-account posting paths changed, OR changes were verified with `dry_run` first
- [ ] Docs updated if user-facing behavior, CLI commands, or MCP tools changed (README, docs/MCP_TOOLS.md, CHANGELOG.md)
- [ ] No secrets, credentials, or `~/.xpst` artifacts in the diff

### Evidence

```text
<!-- paste relevant command output here -->
```

## Notes for Reviewers

<!-- Anything that needs extra eyes: edge cases, follow-ups, known limitations. Delete if empty. -->
