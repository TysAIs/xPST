// The live-chat panel's contract (docs/CHAT-MIXING.md).
//
// Three things a screenshot cannot protect, and this suite does: that the panel
// renders the engine's canonical timeline instead of a second shape, that a
// quiet chat never reads as a broken one (and vice versa), and that the page is
// built for a 390 px phone with 44 px targets. It also compiles the component,
// so a Svelte syntax error fails the node gate rather than only the build.

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { test } from "node:test";
import { compile } from "svelte/compiler";

import {
  BUFFER_LIMIT,
  CHAT_PLATFORMS,
  CHAT_TARGET_PX,
  MAX_ROUNDS,
  WINDOW_CHOICES_S,
  channelPayload,
  channelsToText,
  countRows,
  feedVerdict,
  mergeMessages,
  messageKey,
  platformStatusRows,
  roundsLabel,
  textToChannels,
  timeLabel,
  timelineRows,
} from "../src/lib/chat.js";
import { currentRoute } from "../src/lib/api.js";

const UI_ROOT = resolve(import.meta.dirname, "..");

async function text(path) {
  return readFile(join(UI_ROOT, path), "utf8");
}

const MESSAGE = (overrides = {}) => ({
  platform: "twitch",
  author: "someone",
  text: "hello",
  timestamp: "2026-11-16T13:05:01+00:00",
  message_id: "msg-1",
  channel: "examplechannel",
  ...overrides,
});

test("chat rows are the engine's message fields, in one canonical shape", () => {
  const rows = timelineRows([MESSAGE()]);
  assert.equal(rows.length, 1);
  assert.deepEqual(Object.keys(rows[0]).sort(), [
    "author",
    "channel",
    "key",
    "platform",
    "platformLabel",
    "text",
    "time",
    "timestamp",
  ]);
  assert.equal(rows[0].platform, "twitch");
  assert.equal(rows[0].platformLabel, "Twitch");
  assert.equal(rows[0].author, "someone");
  assert.equal(rows[0].text, "hello");
  assert.equal(rows[0].key, "twitch:msg-1");
  // The panel renders time, platform, author and text — nothing else is
  // required to draw a row, and nothing is invented.
  assert.match(rows[0].time, /^\d{2}:\d{2}:\d{2}$/);
});

test("a frozen-out timestamp is shown as unknown, never redated to read time", () => {
  assert.equal(timeLabel(""), "—");
  assert.equal(timeLabel(undefined), "—");
  assert.equal(timeLabel("not-a-date"), "—");
  // Unknown times sort last, so an unstamped message cannot jump the queue.
  const rows = timelineRows([
    MESSAGE({ message_id: "a", timestamp: "" }),
    MESSAGE({ message_id: "b", timestamp: "2026-11-16T13:00:00+00:00" }),
    MESSAGE({ message_id: "c", timestamp: "2026-11-16T13:00:02+00:00" }),
  ]);
  assert.deepEqual(
    rows.map((row) => row.key),
    ["twitch:b", "twitch:c", "twitch:a"]
  );
});

test("bounded reads merge into one ordered feed without duplicating a re-served message", () => {
  const first = [MESSAGE({ message_id: "1", timestamp: "2026-11-16T13:00:01+00:00" })];
  // The platform re-serves message 1 in the next window; only 2 is new.
  const second = [
    MESSAGE({ message_id: "1", timestamp: "2026-11-16T13:00:01+00:00" }),
    MESSAGE({ platform: "kick", message_id: "9", timestamp: "2026-11-16T13:00:02+00:00" }),
  ];
  const merged = mergeMessages(first, second);
  assert.equal(merged.messages.length, 2);
  assert.equal(merged.added, 1);
  assert.equal(merged.dropped, 0);
  assert.deepEqual(
    merged.messages.map((message) => messageKey(message)),
    ["twitch:1", "kick:9"]
  );

  // A message with no platform id still de-duplicates on its content.
  const again = mergeMessages(merged.messages, [
    MESSAGE({ platform: "kick", message_id: "", author: "x", text: "no id" }),
  ]);
  const duplicate = mergeMessages(again.messages, [
    MESSAGE({ platform: "kick", message_id: "", author: "x", text: "no id" }),
  ]);
  assert.equal(duplicate.added, 0);
  assert.equal(duplicate.messages.length, 3);
});

test("the client buffer is bounded, and says what it dropped", () => {
  const many = Array.from({ length: BUFFER_LIMIT + 5 }, (_, index) =>
    MESSAGE({ message_id: `m${index}`, timestamp: `2026-11-16T13:00:${String(index % 60).padStart(2, "0")}+00:00` })
  );
  const merged = mergeMessages([], many);
  assert.equal(merged.messages.length, BUFFER_LIMIT);
  assert.equal(merged.dropped, 5);
});

test("the platform strip keeps the engine's code and next step per platform", async () => {
  const readiness = {
    read_only: true,
    platforms: [
      {
        platform: "twitch",
        status: "ready",
        channels: ["examplechannel"],
        action: "Twitch chat is read anonymously.",
        verified: true,
      },
      {
        platform: "youtube",
        status: "unavailable",
        channels: [],
        action: "Connect YouTube, then start a live broadcast.",
        unavailable: {
          platform: "youtube",
          code: "credentials_missing",
          message: "youtube: missing oauth_token",
          action: "Connect YouTube, then start a live broadcast.",
        },
      },
      {
        platform: "kick",
        status: "unavailable",
        channels: [],
        action: "Set a Kick channel slug.",
        unavailable: {
          platform: "kick",
          code: "no_channel",
          message: "kick: no channel is configured, so there is no chat to read",
          action: "Set a Kick channel slug.",
        },
      },
    ],
  };
  const rows = platformStatusRows(readiness, null);
  assert.deepEqual(rows.map((row) => row.platform), ["twitch", "youtube", "kick"]);
  assert.equal(rows[0].channels, "examplechannel");
  assert.equal(rows[0].code, "");
  // A healthy row carries no refusal message and no "Next:" advice — the
  // registry note is a description of the path, not an error to act on.
  assert.equal(rows[0].ready, true);
  assert.equal(rows[0].message, "");
  assert.match(await text("src/pages/Chat.svelte"), /row\.action && !row\.ready/);
  // Two different refusals stay two different codes: "no channel" and "no
  // credential" are not the same next step.
  assert.equal(rows[1].code, "credentials_missing");
  assert.equal(rows[2].code, "no_channel");
  assert.match(rows[2].action, /Kick channel/);
  for (const row of rows) assert.ok(row.stateLabel.length > 0);
});

test("a live read's own verdict outranks readiness, and an empty window is not a refusal", () => {
  const readiness = {
    platforms: [
      { platform: "twitch", status: "ready", channels: ["examplechannel"], action: "" },
      { platform: "kick", status: "unavailable", channels: [], unavailable: { code: "no_channel", action: "Set a slug." } },
    ],
  };
  const live = {
    platforms: [
      { platform: "twitch", status: "empty", messages: 0, channel: "examplechannel" },
      { platform: "kick", status: "unavailable", unavailable: { code: "platform_error", message: "kick: 404" } },
    ],
  };
  const rows = platformStatusRows(readiness, live);
  const twitch = rows.find((row) => row.platform === "twitch");
  const kick = rows.find((row) => row.platform === "kick");
  assert.equal(twitch.state, "empty");
  assert.equal(twitch.stateLabel, "Empty window");
  assert.equal(twitch.code, "");
  assert.equal(twitch.ready, true);
  assert.equal(kick.code, "platform_error");
  assert.equal(kick.stateLabel, "Unavailable");
});

test("the feed verdict never collapses 'cannot read' into 'nothing happened'", () => {
  const readinessConfigured = {
    platforms: [{ platform: "twitch", status: "ready", channels: ["examplechannel"], action: "" }],
  };
  const readinessEmpty = {
    platforms: [
      {
        platform: "twitch",
        status: "unavailable",
        channels: [],
        unavailable: { code: "no_channel", message: "twitch: no channel", action: "Set a login." },
      },
    ],
  };
  const liveEmpty = { platforms: [{ platform: "twitch", status: "empty", messages: 0 }] };
  const liveRefused = {
    platforms: [
      {
        platform: "twitch",
        status: "unavailable",
        unavailable: { code: "network_error", message: "twitch: connection refused", action: "Try again." },
      },
    ],
  };

  // Nothing configured at all.
  assert.equal(feedVerdict({ readiness: { platforms: [] } }).kind, "unconfigured");
  assert.match(feedVerdict({ readiness: { platforms: [] } }).detail, /Set a Twitch or Kick channel/);

  // Configured but not started yet.
  assert.equal(feedVerdict({ readiness: readinessConfigured }).kind, "idle");
  assert.equal(feedVerdict({ readiness: readinessConfigured }).title, "Not watching yet");

  // A read stopped before it returned is an attempt, not "never started".
  const stopped = feedVerdict({ readiness: readinessConfigured, attempted: true });
  assert.equal(stopped.kind, "idle");
  assert.match(stopped.title, /stopped before the window ended/);

  // Read, and genuinely quiet.
  const quiet = feedVerdict({ readiness: readinessConfigured, live: liveEmpty, rounds: 1 });
  assert.equal(quiet.kind, "empty");
  assert.match(quiet.detail, /quiet chat, not a missing channel/);
  assert.doesNotMatch(quiet.title.toLowerCase(), /no chat/);

  // Read, and refused — the code and the next step are carried through.
  const refused = feedVerdict({ readiness: readinessConfigured, live: liveRefused, rounds: 1 });
  assert.equal(refused.kind, "unavailable");
  assert.equal(refused.code, "network_error");
  assert.match(refused.detail, /network_error|connection refused/);

  // A refused read against a config with no channel is still "nothing is set
  // up" — that is the operator's next step, not a network story.
  assert.equal(feedVerdict({ readiness: readinessEmpty, rounds: 1 }).kind, "unconfigured");

  // Messages on screen.
  assert.equal(
    feedVerdict({ readiness: readinessConfigured, live: liveEmpty, rounds: 2, messages: [MESSAGE()] }).kind,
    "live"
  );
});

test("channel inputs round-trip as the config names the engine stores", () => {
  assert.deepEqual(textToChannels("a, b ,, c"), ["a", "b", "c"]);
  assert.deepEqual(textToChannels(""), []);
  assert.equal(channelsToText(["a", "b"]), "a, b");
  assert.equal(channelsToText(undefined), "");
  assert.deepEqual(
    channelPayload({ twitch: " one , two ", kick: "slug", youtubeVideoId: " https://youtu.be/abc123 " }),
    {
      twitch_channels: ["one", "two"],
      kick_channels: ["slug"],
      youtube_video_id: "https://youtu.be/abc123",
    }
  );
});

test("the strip's wording follows the refusal code, not a guess", async () => {
  const { channelLine } = await import("../src/lib/chat.js");
  assert.equal(channelLine({ channels: "examplechannel", code: "" }), "Configured: examplechannel");
  assert.equal(channelLine({ channels: "", code: "no_channel" }), "No channel configured.");
  assert.equal(
    channelLine({ channels: "", code: "credentials_missing" }),
    "No stored credential for this platform."
  );
  assert.equal(channelLine({ channels: "", code: "not_supported" }), "xPST has no chat read path for this platform.");
  assert.equal(channelLine({ channels: "", code: "" }), "");
  const source = await text("src/pages/Chat.svelte");
  assert.match(source, /channelLine\(row\)/);
});

test("counts and round copy come from the engine's numbers", () => {
  // Counts describe what is rendered, so they always add up to the row total —
  // the engine's own window count is carried by the round note instead.
  assert.deepEqual(
    countRows([
      { platform: "twitch" },
      { platform: "twitch" },
      { platform: "kick" },
      { platform: "unknown" },
    ]),
    [
      { platform: "twitch", label: "Twitch", count: 2 },
      { platform: "kick", label: "Kick", count: 1 },
    ]
  );
  assert.deepEqual(countRows([]), []);
  assert.deepEqual(countRows(undefined), []);
  assert.equal(roundsLabel(0), `0 of ${MAX_ROUNDS} rounds`);
  assert.equal(roundsLabel(MAX_ROUNDS + 5), `${MAX_ROUNDS} of ${MAX_ROUNDS} rounds`);
});

test("saving a new channel set clears the rows read from the old one", async () => {
  // Messages from a channel the panel no longer watches must not sit beside the
  // new configuration, and an in-flight read of the old set must not land.
  const source = await text("src/pages/Chat.svelte");
  const save = source.slice(source.indexOf("async function saveChannels()"), source.indexOf("async function startWatching()"));
  assert.match(save, /generation \+= 1;/);
  assert.match(save, /watching = false;/);
  assert.match(save, /messages = \[\];/);
  assert.match(save, /live = null;/);
  assert.match(save, /Cleared \$\{cleared\}/);
});

test("the panel's constants are the phone targets and the canonical platforms", () => {
  assert.equal(CHAT_TARGET_PX, 44);
  assert.deepEqual(CHAT_PLATFORMS, ["twitch", "youtube", "kick"]);
  // Bounded reads only: a real network window per round, with a ceiling.
  assert.ok(MAX_ROUNDS > 0 && MAX_ROUNDS <= 60);
  assert.ok(WINDOW_CHOICES_S.every((seconds) => seconds > 0 && seconds <= 300));
});

test("the Chat page compiles and carries the 44 px phone contract", async () => {
  const source = await text("src/pages/Chat.svelte");
  const { js } = compile(source, { filename: "Chat.svelte", generate: "dom" });
  assert.ok(js.code.length > 0);
  assert.match(source, /min-height: var\(--chat-target\)/);
  assert.match(source, /overflow-x: hidden/);
  assert.match(source, /CHAT_TARGET_PX/);
  // The page must consume the canonical payload through the shared client.
  assert.match(source, /api\.chatReadiness\(\)/);
  assert.match(source, /api\.chatTimeline\(windowSeconds\)/);
  assert.match(source, /api\.setChatChannels\(/);
  // The panel distinguishes the states the payload distinguishes.
  assert.match(source, /feedVerdict\(/);
  assert.match(source, /platformStatusRows\(/);
  assert.match(source, /row\.code/);
  assert.match(source, /verdict\.action/);
});

test("the panel is read-only: no send, reply or moderation control anywhere", async () => {
  const source = await text("src/pages/Chat.svelte");
  // No *control* can express an outbound action. The page's prose says so in
  // words ("cannot post, moderate, delete or reply"), so this checks the
  // buttons rather than the copy.
  const buttons = [...source.matchAll(/<button\b[\s\S]*?<\/button>/g)].map((match) => match[0]);
  assert.ok(buttons.length >= 3, "the panel still has its save/start/stop controls");
  for (const button of buttons) {
    assert.doesNotMatch(button, /send|reply|post message|moderate|timeout|ban\b|delete message/i);
  }
  // No free-text composer either — the only text inputs are the channel settings.
  assert.doesNotMatch(source, /<textarea/);
  const inputs = [...source.matchAll(/<input\b[^>]*\bid="([^"]+)"/g)].map((match) => match[1]);
  assert.deepEqual(inputs, ["chat-twitch", "chat-kick", "chat-youtube"]);
  // And no mutating call other than the channel write.
  const client = await text("src/lib/api.js");
  assert.match(client, /setChatChannels: \(payload\) => postJSON\("\/api\/chat\/channels"/);
  assert.doesNotMatch(client, /chat\/send|sendChat|sendMessage/);
});

test("the chat route is reachable from the shell's navigation", async () => {
  const client = await text("src/lib/api.js");
  assert.equal(currentRoute("#/chat"), "chat");
  assert.match(client, /id: "chat", href: "#\/chat", label: "Live chat"/);
  assert.match(client, /chatTimeline: \(durationS = 20\) =>/);
  assert.match(client, /chatReadiness: \(\) => getJSON\("\/api\/chat"\)/);
  const app = await text("src/App.svelte");
  assert.match(app, /import Chat from "\.\/pages\/Chat\.svelte"/);
  assert.match(app, /chat: Chat/);
  const nav = await text("src/lib/components/Nav.svelte");
  assert.match(nav, /"messages-square": MessagesSquare/);
});

test("the platform icons cover the chat platforms without a new dependency", async () => {
  const icon = await text("src/lib/components/PlatformIcon.svelte");
  assert.match(icon, /twitch: Radio/);
  assert.match(icon, /kick: Gamepad2/);
  const packageJson = JSON.parse(await text("package.json"));
  const iconPackage = packageJson.dependencies["@lucide/svelte"] ?? packageJson.dependencies["lucide-svelte"];
  assert.ok(iconPackage, "the existing icon dependency is the only icon source");
});
