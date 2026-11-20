# Analytics fetch-side fixtures (issue #298)

Everything here was captured from the live provider surfaces on 2026-11-17 while
triaging "live analytics collection returns zero metrics". Ids, handles and
cursor tokens are fictional replacements; nothing identifying survives.

| file | what it is | provenance |
| --- | --- | --- |
| `x_tweet_detail_deleted.json` | `TweetDetail` body for a post the account can no longer see: the focal entry exists, `tweet_results` is `{}` | **Captured verbatim** from the cookie-authenticated session (HTTP 200, `application/json`, 957 bytes). Same shape returned for all 21 ids recorded in `state.json`; ids/cursor replaced. |
| `x_query_not_found_404.txt` | body of a rejected GraphQL query id: `{"message":"Query not found"}` | **Captured verbatim**: `GET https://x.com/i/api/graphql/<bogus-query-id>/TweetDetail` with the real session cookies answered HTTP 404, `text/plain`. |
| `x_tweet_detail_errors_envelope.json` | `{"errors": [...]}` diagnostic envelope | X's documented GraphQL error envelope (200 with an error list). Not captured on this run — the live session's query id was still valid — so it is labelled, not presented as an observation. |
| `instagram_login_wall_200.html` | first 800 bytes of the HTML login page Instagram serves to a request that is not logged in | **Captured verbatim** (truncated): anonymous `GET https://www.instagram.com/api/v1/media/1/info/` answered HTTP 200, `text/html; charset="utf-8"`, 399 873 bytes. This is the body instagrapi tries to parse as JSON, which surfaces as `JSONDecodeError: Expecting value`. |
| `instagram_doc_id_query_error.txt` | the message instagrapi raises when Instagram rejects its hardcoded `doc_id` | Reported in issue #298 from the affected session (`Missing 'data' in doc_id GraphQL response … Summary: 'Query Error'`); kept verbatim as the string the classifier must recognise. |

No fixture is produced by a test-time network call: tests load these files.
