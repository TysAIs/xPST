// The live-chat panel's pure half: row/status/verdict construction for the
// merged, read-only timeline served by `GET /api/chat`.
//
// Why this is a module instead of markup: the panel must not invent a second
// payload shape (the engine already returns one canonical timeline), and it
// must not let "nothing arrived" and "cannot be read" look the same — those are
// the two failures the chat pillar exists to prevent. Both are decisions over
// the engine's own fields, so they live here where a node test can hold them.

import { platformLabel } from "./labels.js";

/** Same phone target the rest of the app measures against (44 px). */
export const CHAT_TARGET_PX = 44;
/** The canonical read window choices, in seconds. */
export const WINDOW_CHOICES_S = [10, 20, 60];
export const DEFAULT_WINDOW_S = 20;
/**
 * How many bounded reads a single "watching" session performs before it stops
 * on its own. Each read is one real network window, so this is the ceiling that
 * keeps a forgotten tab from reading forever. A person presses Start again.
 */
export const MAX_ROUNDS = 20;
/** Client buffer ceiling — the engine's own default timeline bound. */
export const BUFFER_LIMIT = 500;
/** Presentation order, matching the engine's registry order. */
export const CHAT_PLATFORMS = ["twitch", "youtube", "kick"];

const EMPTY_TIME = "—";

/**
 * The identity of one message, for de-duplication across bounded windows.
 *
 * Two consecutive reads of the same channel overlap: the platform re-serves
 * recent messages, so without this the panel would show the same line twice
 * and read as a bug. Mirrors the engine's `ChatMessage.identity`.
 */
export function messageKey(message) {
  const platform = String(message?.platform ?? "");
  const id = String(message?.message_id ?? "");
  if (id) return `${platform}:${id}`;
  return [
    platform,
    String(message?.channel ?? ""),
    String(message?.author ?? ""),
    String(message?.timestamp ?? ""),
    String(message?.text ?? ""),
  ].join(":");
}

/**
 * Merge one bounded read into the accumulated timeline.
 *
 * Returns the merged list plus how many rows were new, so the panel can say
 * what this round added instead of implying the window replaced the feed.
 * Ordering is the engine's: platform timestamp first, unknown times last.
 */
export function mergeMessages(existing = [], incoming = [], limit = BUFFER_LIMIT) {
  const priorKeys = new Set((existing ?? []).map((message) => messageKey(message)));
  const seen = new Set();
  const merged = [];
  let added = 0;
  let dropped = 0;
  for (const message of [...(existing ?? []), ...(incoming ?? [])]) {
    const key = messageKey(message);
    if (seen.has(key)) continue;
    seen.add(key);
    merged.push(message);
    if (!priorKeys.has(key)) added += 1;
    if (merged.length > limit) {
      merged.shift();
      dropped += 1;
    }
  }
  return { messages: sortMessages(merged), added, dropped };
}

/** Chronological order with the engine's "unknown time sorts last" rule. */
export function sortMessages(messages = []) {
  return [...messages].sort((left, right) => {
    const a = String(left?.timestamp ?? "");
    const b = String(right?.timestamp ?? "");
    if (a === b) return messageKey(left).localeCompare(messageKey(right));
    if (!a) return 1;
    if (!b) return -1;
    return a.localeCompare(b);
  });
}

/**
 * One row as the panel renders it: time, platform, author, text.
 *
 * The time is the platform's own stamp in the operator's local clock — never
 * the moment xPST read the message, which is why an unstamped message shows
 * "—" instead of being re-dated to now.
 */
export function timelineRows(messages = []) {
  return sortMessages(messages).map((message) => ({
    key: messageKey(message),
    platform: String(message?.platform ?? ""),
    platformLabel: platformLabel(message?.platform),
    author: String(message?.author ?? ""),
    text: String(message?.text ?? ""),
    timestamp: String(message?.timestamp ?? ""),
    time: timeLabel(message?.timestamp),
    channel: String(message?.channel ?? ""),
  }));
}

/** `HH:MM:SS` in the reader's local time, or "—" when the platform gave none. */
export function timeLabel(timestamp) {
  const raw = String(timestamp ?? "").trim();
  if (!raw) return EMPTY_TIME;
  const parsed = new Date(raw);
  if (Number.isNaN(parsed.getTime())) return EMPTY_TIME;
  return parsed.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

function readinessByPlatform(readiness) {
  const rows = new Map();
  for (const row of readiness?.platforms ?? []) {
    if (row?.platform) rows.set(String(row.platform), row);
  }
  return rows;
}

function liveByPlatform(live) {
  const rows = new Map();
  for (const row of live?.platforms ?? []) {
    if (row?.platform) rows.set(String(row.platform), row);
  }
  return rows;
}

/**
 * The platform strip: readiness (what is configured/credentialled) merged with
 * the last read's own verdict (read / empty / unavailable + code).
 *
 * Readiness is what decides whether a platform *can* be read; the live row is
 * what happened when it was. Both are engine fields — this function only puts
 * them side by side, and keeps the coded refusal attached to the row it belongs
 * to.
 */
export function platformStatusRows(readiness, live) {
  const ready = readinessByPlatform(readiness);
  const read = liveByPlatform(live);
  const platforms = CHAT_PLATFORMS.filter((name) => ready.has(name) || read.has(name));
  const extras = [...new Set([...ready.keys(), ...read.keys()])].filter(
    (name) => !CHAT_PLATFORMS.includes(name)
  );
  return [...platforms, ...extras].map((platform) => {
    const readinessRow = ready.get(platform) ?? {};
    const liveRow = read.get(platform) ?? {};
    const refusal = liveRow.unavailable ?? readinessRow.unavailable ?? null;
    const status = String(liveRow.status ?? readinessRow.status ?? "unknown");
    const messages = Number(liveRow.messages ?? 0);
    const channels = liveRow.channel
      ? String(liveRow.channel)
      : (readinessRow.channels ?? []).join(", ");
    return {
      platform,
      label: platformLabel(platform),
      state: status,
      stateLabel: statusText(status, Boolean(refusal)),
      ready: status === "ready" || status === "read" || status === "empty",
      code: String(refusal?.code ?? ""),
      // Only a refusal has a message of its own. The registry note is a
      // description of the path, not an error, and duplicating it here rendered
      // the same sentence twice on a perfectly healthy row.
      message: String(refusal?.message ?? ""),
      action: String(refusal?.action || readinessRow.action || ""),
      channels,
      messages,
      note: String(readinessRow.note ?? ""),
      verified: Boolean(readinessRow.verified),
    };
  });
}

function statusText(status, refused) {
  if (refused) return "Unavailable";
  switch (status) {
    case "read":
      return "Read";
    case "empty":
      return "Empty window";
    case "ready":
      return "Ready";
    case "unavailable":
      return "Unavailable";
    default:
      return "Unknown";
  }
}

/**
 * What the feed area says, as one verdict.
 *
 * Kinds, in priority order:
 * - `unconfigured`  no channel is set anywhere, so there is nothing to read;
 * - `unavailable`   every requested platform refused, with its code;
 * - `idle`          never watched in this session;
 * - `empty`         read, but no message arrived in the window (not an error);
 * - `live`          messages are on screen.
 *
 * The distinction between `empty` and `unavailable` is the whole point: a quiet
 * chat and a broken read used to render the same "no chat" sentence.
 */
export function feedVerdict({
  readiness,
  live,
  messages = [],
  watching = false,
  rounds = 0,
  attempted = false,
} = {}) {
  const rows = platformStatusRows(readiness, live);
  const configured = rows.some((row) => row.channels.trim().length > 0);
  const refused = rows.filter((row) => row.code);
  const readable = rows.filter((row) => row.state === "read" || row.state === "empty");
  const refusedAll =
    rows.length > 0 && readable.length === 0 && refused.length === rows.length;

  if (messages.length > 0) {
    return {
      kind: "live",
      title: `${messages.length} ${messages.length === 1 ? "message" : "messages"}`,
      detail: watching
        ? `Reading round ${Math.min(rounds, MAX_ROUNDS)} of ${MAX_ROUNDS}.`
        : "Reading is stopped; the messages already read stay on screen.",
    };
  }
  if (!configured) {
    return {
      kind: "unconfigured",
      title: "No channel yet",
      detail:
        "Nothing is configured to watch, so there is nothing to read. Set a Twitch or Kick channel above (YouTube needs a live video id).",
    };
  }
  if (refusedAll) {
    const first = refused[0];
    return {
      kind: "unavailable",
      title: "Chat cannot be read",
      detail: first.message
        ? `${first.label}: ${first.message}${first.action ? ` — ${first.action}` : ""}`
        : `${first.label} refused with ${first.code}.`,
      code: first.code,
      action: first.action,
    };
  }
  if (!watching && rounds === 0) {
    // A read that was stopped before it returned is not "never started": saying
    // so would hide the attempt and leave an operator wondering whether their
    // press registered.
    if (attempted) {
      return {
        kind: "idle",
        title: "Read stopped before the window ended",
        detail:
          "No window completed, so there is nothing to show. Press Start watching to read the configured channels again.",
      };
    }
    return {
      kind: "idle",
      title: "Not watching yet",
      detail:
        "Press Start watching to read the configured channels for one bounded window. Each press reads; nothing is ever sent.",
    };
  }
  return {
    kind: "empty",
    title: "No messages in this window",
    detail:
      "The platform was read and answered with no message in the window. That is a quiet chat, not a missing channel or a failed read.",
  };
}

/** Progress copy for the watch control, bounded by the round ceiling. */
export function roundsLabel(rounds = 0, max = MAX_ROUNDS) {
  return `${Math.min(Math.max(rounds, 0), max)} of ${max} rounds`;
}

/**
 * What the strip says about this platform's configuration.
 *
 * The wording follows the refusal code, because "no channel" and "no
 * credential" are different operator actions: a YouTube row with no token is
 * not missing a channel name, and telling it that would send someone to the
 * wrong screen.
 */
export function channelLine(row) {
  const channels = String(row?.channels ?? "").trim();
  if (channels) return `Configured: ${channels}`;
  switch (row?.code) {
    case "credentials_missing":
      return "No stored credential for this platform.";
    case "no_channel":
      return "No channel configured.";
    case "not_supported":
      return "xPST has no chat read path for this platform.";
    default:
      return "";
  }
}

/**
 * Per-platform counts of what is actually on screen.
 *
 * The engine's `counts` field describes its own bounded window; this panel
 * accumulates consecutive windows, so reusing that number would print "109"
 * beside 190 rendered rows. Counting the rows we hold is the only figure that
 * matches what a person can see, and the per-round note carries the window's
 * own number.
 */
export function countRows(messages = []) {
  const tally = new Map();
  for (const message of messages) {
    const platform = String(message?.platform ?? "");
    if (!platform) continue;
    tally.set(platform, (tally.get(platform) ?? 0) + 1);
  }
  return CHAT_PLATFORMS.filter((platform) => tally.has(platform)).map((platform) => ({
    platform,
    label: platformLabel(platform),
    count: tally.get(platform),
  }));
}

/** Comma-separated channel list ⇄ the array the engine stores. */
export function channelsToText(channels = []) {
  return (channels ?? []).filter(Boolean).join(", ");
}

export function textToChannels(text = "") {
  return String(text ?? "")
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);
}

/** The payload a save sends: only changed fields, in the engine's own names. */
export function channelPayload({ twitch = "", kick = "", youtubeVideoId = "" } = {}) {
  return {
    twitch_channels: textToChannels(twitch),
    kick_channels: textToChannels(kick),
    youtube_video_id: String(youtubeVideoId ?? "").trim(),
  };
}
