
import re, pathlib
p = pathlib.Path("/tmp/xpst-conf/AGENTS.md")
t = p.read_text()
b1 = pathlib.Path("/tmp/xpst-conf/.block1.txt").read_text().rstrip("\n")
b2 = pathlib.Path("/tmp/xpst-conf/.block2.txt").read_text().rstrip("\n")
# replace first conflict block
t = re.sub(r"<<<<<<< HEAD\n.*?=======\n.*?>>>>>>> origin/main\n", b1+"\n", t, count=1, flags=re.S)
# replace second conflict block
t = re.sub(r"<<<<<<< HEAD\n.*?=======\n.*?>>>>>>> origin/main\n", b2+"\n", t, count=1, flags=re.S)
# counts
t = t.replace("exposes 44 top-level commands (66 counting\nsubcommands)", "exposes 46 top-level commands (69 counting\nsubcommands)")
assert "46 top-level" in t, "cli count replace failed"
# stale MCP capability bullet
old = """- **MCP has no schedule-cancel and no targeted retry.** Those live in the CLI
  (`python -m xpst schedule`, `python -m xpst failures`). Prefer adding the MCP tool over
  teaching agents to shell out."""
new = """- **MCP now covers schedule-cancel and targeted retry** (`xpst_schedule_cancel`,
  `xpst_failures_retry`); the CLI equivalents (`python -m xpst schedule`,
  `python -m xpst failures`) remain for scripting. Prefer the MCP tool over teaching
  agents to shell out."""
assert old in t, "mcp bullet not found"
t = t.replace(old, new)
assert "<<<<<<<" not in t and ">>>>>>>" not in t
p.write_text(t)
print("ok")
