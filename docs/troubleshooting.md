# Troubleshooting

This section is being updated. For platform-specific issues right now, use the individual setup docs under `docs/`:
- [YouTube setup](setup-youtube.md)
- [Instagram setup](setup-instagram.md)
- [X/Twitter setup](setup-x-twitter.md)
- [TikTok setup](setup-tiktok.md)
- [Threads setup](setup-threads.md)
- [Messenger setup](setup-messenger.md)

For general deploy/integration guidance, see [INSTALL.md](INSTALL.md) and [QUICKSTART.md](QUICKSTART.md).

## First-run and failure behaviour (what xPST does on purpose)

These are the deterministic behaviours every entry point implements; each one
has a regression test, so "it did something else" is a bug worth reporting.

### "xPST is offline" — no network on first run

`doctor`, `health` and `run` probe connectivity (DNS + a TCP connect) and say so
in plain language:

* `xpst doctor` lists `network` under **Environment** (`ok: false`,
  `detail: "offline — DNS resolution failed for …"`) and adds a fix-it entry.
* `xpst health --json` always carries a `network` object
  (`{"online": false, "detail": "…"}`).
* `xpst run` reports `status: "offline_no_network"` and never claims a post
  succeeded. Local state stays readable, so `status`/`logs`/`state export`
  keep working with no network at all.

Nothing is posted while offline: an upload that cannot reach the provider is
recorded as a **failure** (with the provider error) in `state.json`
(`posted_videos.<id>.errors.<platform>`), never as a publish, so a later retry
publishes exactly once.

### Config directory is missing, read-only or not writable

* The directory is created on first run (`0700`) together with
  `config.yaml` (`0600` — it holds API tokens).
* If it exists but is not writable, or the path is a file, xPST stops with
  `Configuration error: XPST config directory … is not writable … Fix: chmod
  u+rwx … or set XPST_CONFIG_DIR to a writable directory.` and exit code 2.
  There is no traceback.
* Point `XPST_CONFIG_DIR` anywhere to keep a profile away from `~/.xpst`; the
  CLI, desktop app, scheduler and migrator all honour it.

### Corrupted `config.yaml` or `state.json`

xPST never silently discards a file it cannot read:

* unparseable YAML / non-UTF-8 / empty config → the bad file is copied to
  `backups/config.yaml.corrupt_<epoch>` and xPST exits 2 with the backup path in
  the message;
* unreadable `state.json` → the bytes are quarantined to
  `backups/corrupted_<epoch>_<hash>.json`, a `.json.forensic` copy is kept, and
  state is recovered from the newest `backups/state_*.json` when one exists.

### A second instance is already running

Single-instance behaviour is a lock, never a silent exit:

| Entry point | Behaviour when an instance is live |
| --- | --- |
| `xpst serve` (daemon) | exits **0** immediately after logging "another instance is running" — idempotent for cron/launchd keep-alive |
| `xpst run` / `xpst watch` (one-shot) | exits **1** with "Another xPST instance is already running" |
| desktop app (`xpst app`) | the second launch prints "xPST is already running." and exits **10**, leaving the first window in place |
| packaged engine sidecar | refuses the port (see below) and exits when its parent shell dies, so it can never outlive the app |

A pidfile left behind by a **crashed** process never blocks a fresh launch: the
advisory lock is released by the kernel, and the new instance overwrites the
stale `xpst.pid`.

### "port N is already in use"

A requested port that is already bound is a hard error — xPST never binds a
different port silently and never keeps running with a dead dashboard:

```
xpst-engine: port 8080 is already in use on 127.0.0.1. Stop the process bound
to it (macOS/Linux: `lsof -ti tcp:8080 | xargs kill`) or pass `--port` with a
free port.                      # exit code 3
```

`xpst serve --port N` performs the same pre-flight and exits **3** with the same
message before the scheduler starts. Choose another port with `--port` or
`XPST_DASHBOARD_PORT`.
