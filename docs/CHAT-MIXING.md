# Live chat: one merged, read-only timeline

xPST reads live chat from **Twitch, YouTube and Kick** and returns a single
ordered timeline. Every message carries the platform it came from, the author,
the text, the platform's own timestamp and message id, and the channel — so an
operator watching one screen sees all three chats interleaved instead of
switching panes.

It is **read-only by construction**. Nothing in this pillar can send a message,
delete one, time someone out, or auto-reply. An anonymous Twitch connection has
no write permission at all; the YouTube and Kick readers only call list and
subscribe operations. Posting into a live chat is a separate decision with its
own consent story, and it is not in this code.

```bash
xpst chat platforms                       # what can be read, and what is missing
xpst chat timeline --twitch-channel <login> --duration 30
xpst chat timeline --platform twitch --twitch-channel a,b --json
xpst chat timeline --kick-channel <slug> --youtube-video <video id>
```

The same payload is served by the `xpst_chat` MCP tool (`{"read": true, ...}`)
and `GET /api/chat?read=true`, so a human, an agent and the web UI cannot
disagree about what was seen.

## The per-platform matrix

| Platform | Transport | Credentials needed | Status |
|----------|-----------|--------------------|--------|
| **Twitch** | IRC over TLS `irc://irc.chat.twitch.tv:6697` (`wss://irc-ws.chat.twitch.tv:443` is the same protocol) | **none** — an anonymous `NICK justinfan<digits>` connection reads public chat | **proven live**: anonymous connection, channel join and real `PRIVMSG` traffic with `display-name`/`id`/`tmi-sent-ts` tags |
| **YouTube** | Data API `GET /youtube/v3/liveChatMessages?liveChatId=…` | the OAuth token the operator already has, with `youtube.readonly` or `youtube.force-ssl`, **and a broadcast that is live right now** | documented, not yet exercised from this repository: the connected channel answers `403 not enabled for live streaming`, so the read refuses with `live_not_enabled` |
| **Kick** | Kick's own realtime service `wss://realtime.<region>.platform.kick.com/connection/websocket`, channel `chatrooms.<id>.v2` | **none** — an anonymous guest token plus a public channel subscription | connection, subscription and heartbeat **proven live**; no chat publication has been observed yet, so the message payload is parsed defensively and the row stays unverified |

### Kick: what is actually true

Kick's published API documents chat only as writes — `POST /public/v1/chat` to
send and `DELETE /public/v1/chat/{id}` to moderate. There is **no read
endpoint**, and no history. The documented push alternative is the
`chat.message.sent` *webhook* event, which needs a registered Kick app and a
publicly reachable callback.

Community libraries still point at a public Pusher app key
(`32cbd69e4b950bf97679`). That path is **dead**: Pusher answers
`4001 App key … not in this cluster` on every cluster. What kick.com's own page
does today, captured from the live page on 2026-11-16, is:

1. `POST https://web.kick.com/api/v1/realtime/auth/connection` with
   `{"client_id": "<uuid>"}` and `x-app-platform: web` returns an anonymous
   *guest* token (`sub: guest:<uuid>`, ~45 min TTL). No account, no
   registration, no secret.
2. Connect the realtime socket and send `{"connect": {"token": …}, "id": 1}`.
3. `{"subscribe": {"channel": "chatrooms.<chatroom id>.v2"}, "id": N}`.
4. The server sends `{}` as a heartbeat and expects `{}` back; ignoring it gets
   the connection closed.

The chatroom id comes from Kick's public channel payload
(`https://kick.com/api/v2/channels/<slug>` → `chatroom.id`).

This is a vendor-internal path: undocumented, unversioned, and free to change
without notice. When it breaks, the refusal says `unverified_path` and names the
documented webhook alternative rather than reporting a mystery network error.

## Why "unavailable" is not "empty"

All three platforms can fail in a way that looks exactly like a quiet chat:

- Twitch silently drops a `JOIN` for a channel that does not exist;
- Kick answers 404 for an unknown slug;
- YouTube has no chat id at all unless a broadcast is live.

So every reader raises a coded refusal and the payload reports it beside the
platforms that did answer:

| Code | Meaning | Next step |
|------|---------|-----------|
| `no_channel` | no channel configured, or the platform would not confirm one | set `chat.*_channels` or pass `--twitch-channel` / `--kick-channel` |
| `credentials_missing` | YouTube has no OAuth token | `xpst auth youtube` |
| `not_live` | YouTube has no live broadcast, so there is no chat | go live, or name a live video |
| `live_not_enabled` | the channel is live-capable-and-refused: 403 "not enabled for live streaming" | verify the phone number at youtube.com/features (a channel setting, not OAuth) |
| `unverified_path` | the undocumented Kick path moved or refused | see the webhook alternative in the refusal |
| `platform_error` / `network_error` / `invalid_response` | the vendor or the wire failed | quoted vendor text, redacted |

The last two distinctions are load-bearing: on this host YouTube answers
`403 The user is not enabled for live streaming`, which is `live_not_enabled`
(the token works, the channel setting does not) and not an auth failure. A
malformed request (`400`) is reported as an xPST defect, never as
"re-authorise" — that mistake was made during this slice's own live testing.

`ok: true` means the read itself ran. An empty `messages` list with an empty
`unavailable` list is a genuinely quiet chat; anything else is explained.

## Quota and cost

YouTube's `liveChatMessages.list` costs **5 quota units per poll** against the
10,000-unit daily default, so 2,000 polls is a whole day. The reader floors its
interval at 2 seconds, honours the vendor's `pollingIntervalMillis`, stops at
`offlineAt`, and reports `"N poll(s), M quota unit(s)"` in the payload note. A
30-second window at the floor is ~15 polls = 75 units.

Twitch and Kick cost nothing: there is no credential and no metered API.

## Configuration

```yaml
chat:
  twitch_channels: []      # logins, e.g. ["somechannel"]
  kick_channels: []        # slugs
  youtube_video_id: ""     # empty = the channel's active broadcast
  max_messages: 500        # the merged timeline is bounded; oldest drop first
  youtube_poll_interval_s: 2.0
```

Explicit arguments win over the file, and an explicitly empty list means "none"
rather than "use the config".

## Design notes

- **No new runtime dependency.** Twitch is IRC over TLS (`asyncio` only),
  YouTube is the Data API client the project already ships, and Kick's socket is
  spoken by `xpst/chat/ws.py`, a minimal RFC 6455 client (~300 lines). Adding a
  WebSocket library for one platform was not worth the licence and supply-chain
  surface; the frame layer is proven against RFC vectors and a real loopback
  server instead.
- **Everything is injectable.** `read_twitch_chat(connect=…)`,
  `read_youtube_chat(list_messages=…)` and
  `read_kick_chat(http_get=…, http_post=…, ws_connect=…)` let the whole suite run
  with no network, which is how the refusal codes are proved on a host with no
  chat credentials at all.
- **Messages sort by the platform's own timestamp**, never by read time. A
  message with no usable timestamp sorts last instead of being redated.

## The web panel (`#/chat`)

The operator-facing half of this pillar is `ui/src/pages/Chat.svelte`, reachable
from the sidebar under **Live → Live chat**. It is one screen with four cards
and no send control of any kind.

- **Platforms.** One row per platform, built from `GET /api/chat`: the
  transport, the configured channel(s), and either the engine's `ready` verdict
  or the coded refusal *with its next step*. The row's wording follows the code
  — a YouTube row with no token says "no stored credential", not "no channel".
- **Channels.** Twitch logins, Kick slugs and a YouTube video id (a watch URL
  is reduced to its id on save). Saving writes `chat.twitch_channels`,
  `chat.kick_channels` and `chat.youtube_video_id` through the engine's ordinary
  config path (`POST /api/chat/channels`, token-guarded, the same call the
  onboarding screen uses), then re-renders the strip from the readiness payload
  the write returns. Nothing is kept in browser storage, so the CLI, MCP and the
  panel cannot disagree about which channel is watched.
- **Watch.** Each press opens one bounded read window (`10 s`, `20 s` or `60 s`)
  against `GET /api/chat?read=true&duration_s=<n>`, and the panel accumulates
  those windows into one ordered feed — de-duplicated by the platform's message
  id, bounded at 500 rows, with the dropped count shown. A session stops itself
  after 20 reads so a forgotten tab cannot read forever; Stop ends it
  immediately and an in-flight reply that lands afterwards is discarded.
- **Merged timeline.** Time (local, from the platform's own stamp), platform
  badge, author, text. Saving a new channel set clears the rows read from the
  old one and says so, because those rows belong to a channel the panel no
  longer watches.

The empty states are the reason this screen exists, so they are separate
sentences rather than one "no chat":

| Feed state | What it says |
|---|---|
| nothing configured | "No channel yet" + which field to set |
| every platform refused | "Chat cannot be read" + the engine's code and next step |
| read, no message in the window | "No messages in this window — a quiet chat, not a missing channel or a failed read" |
| read stopped before the window ended | "Read stopped before the window ended" (an attempt, not "never started") |
| messages on screen | the rows, with the round counter |

Measured on a 390 px viewport against a real local engine (uvicorn on a scratch
config dir, the built bundle, a real Twitch connection): `scrollWidth` 375 ≤
390, every visible control exactly 44 px tall, message rows 63–114 px tall,
`tooSmall: []`. No new npm dependency — the platform icons reuse the existing
`@lucide/svelte` set.

## Not built yet (deliberately)

1. **Sending / moderation / auto-reply.** A write path into someone's live chat
   is a separate product decision (consent tier, rate limits, abuse story).
2. **Chat history.** None of these transports page backwards over a stream;
   the timeline is what was read during the window.
3. **Streaming push instead of repeated bounded reads.** There is no SSE or
   WebSocket route for chat yet: the panel performs bounded reads, which is the
   same call the CLI and MCP make, so all three surfaces see identical windows.
