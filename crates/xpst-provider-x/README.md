# xpst-provider-x

The X destination for the Rust engine: chunked media upload, post creation,
cost preflight and the cross-account duplicate rule.

Everything this crate sends crosses a [`Transport`] trait
(`src/transport.rs`), so the whole publishing path runs against a fake in
`cargo test` and no HTTP client is in the dependency graph. Requests are data,
responses are data, and the tests assert the protocol rather than X's
availability.

## Endpoints that return no body

This is the one thing to read before changing the client, because getting it
wrong is invisible in production until media posts stop working:

| Endpoint | Method | Success status | Body |
|---|---|---|---|
| `media/upload.json` `command=INIT` | POST | 202 | **JSON** — `media_id`, `media_id_string` |
| `media/upload.json` `command=APPEND` | POST | **204** | **none — zero bytes** |
| `media/upload.json` `command=FINALIZE` | POST | 200 | **JSON** — `media_id`, `processing_info` when the media has to process |
| `media/upload.json` `command=STATUS` | GET | 200 | **JSON** — `processing_info` |
| `2/tweets` | POST | 201 | **JSON** — `data.id` |

**`APPEND` is the only endpoint that returns no body.** It answers
`204 No Content`, and that is a legitimate success: the server accepted the
segment and there is nothing to read. Requiring JSON from it — which this crate
did until the audit — fails on the very first segment against the live API, so
*every* media post fails while every fake test passes.

Conversely, an empty body from `INIT`, `FINALIZE`, `STATUS` or `POST 2/tweets`
is **not** success: those bodies carry the media id or post id that the next
step needs, so an empty or unparseable body there is an error
(`X_API_ERROR`), never a shrug. The distinction is per call site
(`SuccessShape::Json` vs `SuccessShape::NoBody` in `src/client.rs`), not a
global "be lenient" or "be strict" flag.

If X adds another command endpoint, decide its shape explicitly and add it to
the table above. Do not widen `SuccessShape::NoBody` to make a failing test go
green.

## Media is only posted once X says it is processed

`FINALIZE` may answer while the media is still being processed:

```json
{"media_id": 123, "processing_info": {"state": "pending", "check_after_secs": 5}}
```

Only `processing_info.state == "succeeded"` means usable. The client therefore:

* posts immediately when the state is `succeeded`;
* waits `check_after_secs` and polls `command=STATUS` for `pending`,
  `in_progress`, or any unrecognised state;
* gives up after `MAX_PROCESSING_POLLS` (20) polls with
  `X_MEDIA_UNCONFIRMED`, and does not post;
* reports a `failed` state immediately, quoting X's own `error_name`, and does
  not post.

A **missing** `processing_info` is treated as *not ready*: it is polled like
`pending`, and if no explicit `succeeded` arrives within the bound the upload is
reported as unconfirmed rather than posted. "X said nothing" is not the same as
"X said it is ready".

Waits go through the `Wait` trait, so tests prove the wait happens without
spending it (`RecordedWaits` in `tests/publish_path.rs`).

## What this crate refuses before sending

Every refusal is decided before the first network call, carries a stable `X_*`
code, and names the number where one exists:

| Refusal | Code |
|---|---|
| text over 280 characters, with the measured count | `X_TEXT_TOO_LONG` |
| empty text post | `X_TEXT_EMPTY` |
| media post without an asset key | `X_ASSET_KEY_REQUIRED` |
| same asset already posted to another account | `X_DUPLICATE_ACROSS_ACCOUNTS` |
| cost over the caller's cap | `X_COST_CAP_EXCEEDED` |
| empty media file | `X_EMPTY_MEDIA` |
| media never confirmed processed (after the bound) | `X_MEDIA_UNCONFIRMED` |

X's own answers are translated, not flattened: 401 is `X_NEEDS_REAUTH`, 429 is
`X_RATE_LIMITED` with the `x-rate-limit-reset` epoch second, and 403/187 is
`X_DUPLICATE_POST`. Nothing is truncated to fit a limit.

## Layout

| Module | What it is |
|---|---|
| `client` | `XPublisher::publish` — the order of every check and every byte sent |
| `transport` | `Transport`, `Request`, `Response`, `Body` — the seam |
| `oauth1` | HMAC-SHA1 OAuth 1.0a signing, pinned against Python-generated vectors |
| `media` | 5 MiB segmentation, streamed from disk, never loaded whole |
| `cost` | the $0.015 / $0.200 tiers and the caller's cap |
| `duplicate` | the publication ledger and the cross-account policy |
| `preflight` | pricing a batch without sending it |

## Tests

`cargo test -p xpst-provider-x` — none of it opens a socket.

`tests/publish_path.rs` models the *real* response shapes, including
`an_append_returning_204_with_no_body_is_a_success` (the regression test for the
204 bug), a finalize that returns JSON, a pending state that must be waited on
and polled, a state that never finishes, a missing `processing_info`, and empty
or unparseable bodies on the JSON endpoints. A fake that answers every 2xx with
JSON cannot fail the way the live API would; that is how the original bug
survived.
