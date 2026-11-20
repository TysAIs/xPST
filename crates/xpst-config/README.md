# xpst-config

Read path for `~/.xpst/config.yaml`: YAML parsing, Python-compatible secret
masking, read-only validation, and the typed account sections the Rust engine
needs in order to connect a destination.

Nothing in this crate writes a config file. There is no migration path here and
no `save()`; P0 established that, and the account sections do not change it.

## Account sections

Two destinations take a **static secret** — an app password or a personal
access token — rather than an OAuth redirect. Their config sections are typed in
`src/accounts.rs`:

```yaml
accounts:
  bluesky:
    enabled: true
    identifier: name.bsky.social       # handle or DID; not a secret
    service: https://bsky.social       # self-hosted PDS override
    credential_ref: bluesky_app_password
  mastodon:
    enabled: true
    instance: https://mastodon.social  # origin only: no path, no trailing slash
    credential_ref: mastodon_access_token
```

| Section | Field | Meaning |
|---|---|---|
| `accounts.bluesky` | `enabled` | Off by default, exactly as in the Python engine. |
| | `identifier` | Handle or DID. Required once `enabled` is true. |
| | `service` | PDS origin. Default `https://bsky.social`. |
| | `credential_ref` | Credential-store key holding the app password. |
| `accounts.mastodon` | `enabled` | Off by default. |
| | `instance` | Instance origin. Required once `enabled` is true. |
| | `credential_ref` | Credential-store key holding the access token. |

`credential_ref` is the only field this engine added to the platform set; a
blank value falls back to the default key, so an older config that omits it
still resolves.

## One config file, and what Python does with it

The two engines read the same file. That is a requirement, not a coincidence,
and it cuts both ways:

* **`accounts.bluesky` already exists in Python's schema** (`src/xpst/config.py`,
  `BlueskyAccountConfig`), with the same `enabled` / `identifier` / `service`
  fields and the same defaults. This crate reads those keys and nothing else
  that Python owns.
* **`accounts.mastodon` has no Python equivalent.** Python has no Mastodon
  destination at all, so there was no section name to match and none to
  collide with: `mastodon` appears in no Python default, no Python migration
  version and no Python account reader. The name was chosen to be obviously
  about the destination rather than borrowed from another platform.
* **Python ignores both additions, and drops them when it rewrites the file.**
  `XPSTConfig.save()` serialises a fixed field set, so an unknown key
  (`credential_ref`) and an unknown section (`accounts.mastodon`) survive a
  *read* but not a Python *write*. That is why `credential_ref` defaults to the
  exact store key Python itself uses (`bluesky_app_password`): after such a
  rewrite the reference resolves identically. A Mastodon section would have to
  be re-added after a Python rewrite — stated here rather than hidden, because
  the alternative would be giving Mastodon a Python-owned name it would then be
  silently dropped from anyway.

## Secrets are not in this file

Neither section has a field that can hold a secret, and
`tests/secrets_stay_out_of_config.rs` asserts the accepted key set so that
adding one fails a test. What the config carries is the *name* of the
credential-store entry.

Storage belongs to [`xpst-authflow`](../xpst-authflow): OS keychain first,
Argon2id + XChaCha20-Poly1305 encrypted file as the explicit fallback. The
default keys are Python's own store keys:

| Platform | Key | Python constant |
|---|---|---|
| Bluesky app password | `bluesky_app_password` | `CRED_APP_PASSWORD` |
| Bluesky cached session | `bluesky_session` | `CRED_SESSION` |
| Mastodon access token | `mastodon_access_token` | — (Rust only) |

The entry *format* inside those files is not yet interchangeable between the
engines (Python uses Fernet + scrypt, this workspace uses Argon2id +
XChaCha20-Poly1305). The smoke reports a Python-written entry as unreadable by
this build rather than guessing, which is the honest outcome until the two
stores are unified.

## Validation

Every error returned by this module ends in the action that fixes it:

| Situation | Message carries |
|---|---|
| Empty `identifier` | Where the handle is shown in Bluesky, and `xpst connect bluesky` |
| Malformed instance URL | Why it cannot work, and the address-bar form to use instead |
| `http://` on a public host | "instances are served over https" |
| A path/query/fragment in an origin | "an instance is an address only" |
| The account password where an app password belongs | Bluesky app passwords are 16 lowercase chars in four dash-separated groups; create one at <https://bsky.app/settings/app-passwords>, and it can be revoked without touching the account password |
| No credential stored | The key name, and the command or settings page that stores one |

`ConfigDocument::validate()` adds one blocking check per *present* section. A
config that never mentions Bluesky or Mastodon gains no checks at all — that is
the common case, and it is asserted by a test.

The app-password shape check is deliberately strict: Bluesky issues app
passwords as `xxxx-xxxx-xxxx-xxxx` (lowercase, 16 characters). Anything else is
refused with that format quoted, because the alternative is storing what is
almost always the account password and letting the PDS reject it on every post.

## The credential lookup

`BlueskyAccount::credential` / `MastodonAccount::credential` take a lookup
closure, so this crate owns the error message without depending on the storage
implementation:

```rust
let credential = account.credential(|key| store.get::<Credential>(key).map_err(|e| e.to_string()))?;
```

`Ok(Some(_))` returns it, `Ok(None)` becomes the actionable "no credential
stored under `<key>`" error, and `Err(detail)` becomes "the credential exists
but could not be read", keeping a damaged store separate from an unconnected
account.
