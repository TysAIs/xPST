# Zernio fixture provenance

These are **documented shapes, not captured exchanges**. No vendor account exists
and no request has been made, so every JSON here is assembled from the vendor's
public documentation as cited by the socials spec
(`work/xpst-aggregator-plugin-spec-2026-11-20.md`, §4 and §5) and the API surface
xPST needs:

| File | Shape it stands for |
|---|---|
| `accounts.json` | `GET /v1/accounts` listing one connected TikTok account |
| `account_health.json` | `GET /v1/accounts/{id}/health` with `canPost` / `needsReconnect` / `privacyLevels` |
| `creator_info_public.json` | creator info that offers `PUBLIC_TO_EVERYONE` |
| `creator_info_no_public.json` | creator info that does not (the refusal case) |
| `capacity_full.json` | a capacity precheck answering `canPublish: false` |
| `presign.json` | a media presign answer with an upload target and a public URL |
| `post_published.json` | a create answer with a resolved public URL |
| `post_published_url_null.json` | `published` with `platformPostUrl: null` — the PENDING case |
| `post_207_failed.json` | `207` (a 2xx that means failure) with a failed TikTok entry |
| `post_401.json` | an authentication failure envelope |
| `posts_listing.json` | `GET /v1/accounts/{id}/posts`, used only for reconciliation |
| `readback_real_page.html` | a public TikTok page carrying a matching `itemStruct` |
| `readback_fake_id_page.html` | the reproduced fake-id page: payload present, **no** `itemStruct` |
| `readback_wrong_id_page.html` | payload whose `itemStruct.id` is a different video |
| `readback_wrong_author_page.html` | payload whose `itemStruct.author.uniqueId` differs |
| `readback_login_wall.html` | a page with no rehydration payload at all |

The first real post (B9) is what confirms field names. If the vendor's actual
response differs, correct `_create_body` / the client methods first and then
re-record these files from the real exchanges; do not widen the tests to accept
both shapes silently.
