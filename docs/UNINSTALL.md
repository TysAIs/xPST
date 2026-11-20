# Uninstalling xPST — what is left behind

xPST has **no uninstaller that deletes user data**: removing the app (or
`pip uninstall xpst`, or dragging `xPST.app` to the Trash) never touches the
config directory. This page is the exact, verified inventory so an uninstall
is a deliberate, complete action rather than a guess.

The inventory below is asserted by
`tests/test_upload_interruption_and_upgrade.py::test_uninstall_inventory_is_documented`
and `::test_credentials_directory_shape_is_stable`.

## Where everything lives

The config directory is one of:

| Platform | Path |
| --- | --- |
| macOS / Linux | `~/.xpst` |
| Windows | `%APPDATA%\xPST` |
| any (override) | `$XPST_CONFIG_DIR` — **wins over both** |

`XPST_CONFIG_DIR` is honored by the CLI, the desktop app, the scheduler and the
config migrator, so an isolated profile really is isolated.

## Inventory after a first run (and after normal use)

| Path (relative to the config dir) | Contents | Permissions |
| --- | --- | --- |
| `config.yaml` | accounts, tokens (`client_secret`, `access_token`, `page_access_token`, Graph tokens), dashboard password **hash**, schedule, notifications | `0600` |
| `state.json` | posted-video history, content hashes, per-platform failure records, platform health | `0644` (no secrets) |
| `.state.lock` | cross-process lock inode (never contains data) | `0644` |
| `xpst.pid` | engine/daemon pidfile (exists only while an instance runs) | `0644` |
| `upload_checkpoints.json` | crash-recovery checkpoints for in-flight uploads | `0644` |
| `quotas.json` | per-platform daily/hourly upload counters | `0644` |
| `analytics.db` | local analytics snapshots | `0644` |
| `analytics_snapshots/` | optional snapshot store | `0644` |
| `logs/xpst.log`, `logs/cron.log`, `logs/launchagent.*` | logs | `0644` |
| `backups/` | `config.yaml.backup_*` (pre-migration copies), `config.yaml.corrupt_*`, `state_*.json`, `corrupted_*.json` | `0644` |
| `credentials/` | encrypted credential blobs and the per-install Fernet material | `0700` dir |
| `credentials/*.enc` | AES/Fernet-encrypted credential values (never plaintext) | `0600` |
| `credentials/.fallback_secret`, `.fallback_salt` | per-install KDF material (random, never derived from hardware IDs) | `0600` |
| `credentials/_keyring_index.json` | key names stored in the OS keychain, when `XPST_USE_KEYRING=1` | `0600` |
| `downloads/` | downloaded source videos (may be large; `video.cleanup_after_post` clears them) | `0755` |
| `thumbnails/`, `cache/` | desktop-app image caches | `0755` |
| `settings.json`, `wizard_state.json` | desktop UI preferences / first-run progress | `0644` |

Nothing outside the config directory is written: no state in `~/Library`,
`/tmp` (beyond OS temp files that are removed), or the app bundle. The only
other location is the **macOS Keychain** when `XPST_USE_KEYRING=1` (entries
under the service name `xpst`) — that survives file deletion and must be
removed from Keychain Access or with
`security delete-generic-password -s xpst` (per entry).

## Credentials: never world-readable

* `config.yaml` **is** written `0600` (it carries API tokens). Regression guard:
  `test_config_file_is_never_world_readable`.
* Everything under `credentials/` is `0600` inside a `0700` directory.
* xPST refuses to write a credential in plaintext: with neither the OS keychain
  nor `cryptography` available it fails loudly (`PlaintextStorageError`) rather
  than persisting a token.
* Guard test: `test_credentials_are_not_left_world_readable` walks the whole
  config directory and fails if a known credential value appears in any file
  that is group/world accessible.

## Removing xPST completely

```bash
# 1. stop anything running
xpst serve --help >/dev/null 2>&1 && pkill -f "xpst" || true

# 2. remove the state (macOS/Linux; delete ~/.xpst for the default location)
rm -rf "${XPST_CONFIG_DIR:-$HOME/.xpst}"

# 3. only if XPST_USE_KEYRING=1 was ever used
security delete-generic-password -s xpst   # macOS, repeat per entry
```

Back up `config.yaml` and `credentials/` first if the install is to be
recreated — deleting them destroys the tokens and the per-install secret (a
copy of `credentials/*.enc` without `.fallback_secret` cannot be decrypted).
