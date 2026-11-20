<script>
  // The "one screen" half of the chat pillar (docs/CHAT-MIXING.md): Twitch,
  // YouTube and Kick chat in a single ordered feed, read-only by construction.
  //
  // Every number and every verdict on this page comes from the engine's own
  // payload — `GET /api/chat` for readiness, `GET /api/chat?read=true` for a
  // bounded read, `POST /api/chat/channels` for the persisted channel list. The
  // page never invents a second shape and never guesses why a feed is quiet:
  // "no messages in this window" and "this platform cannot be read" are
  // different sentences because they are different facts.
  //
  // There is deliberately no send control. An anonymous Twitch connection has
  // no write permission, and the YouTube/Kick readers only list and subscribe.
  import { onDestroy, onMount } from "svelte";
  import { api, errorMessage } from "../lib/api.js";
  import {
    BUFFER_LIMIT,
    CHAT_TARGET_PX,
    DEFAULT_WINDOW_S,
    MAX_ROUNDS,
    WINDOW_CHOICES_S,
    channelLine,
    channelPayload,
    channelsToText,
    countRows,
    feedVerdict,
    mergeMessages,
    platformStatusRows,
    roundsLabel,
    timelineRows,
  } from "../lib/chat.js";
  import Card from "../lib/components/Card.svelte";
  import PlatformBadge from "../lib/components/PlatformBadge.svelte";
  import StatusBadge from "../lib/components/StatusBadge.svelte";

  let readiness = $state(null);
  let readinessError = $state("");
  let live = $state(null);

  let twitchText = $state("");
  let kickText = $state("");
  let videoId = $state("");
  let saving = $state(false);
  let saveNotice = $state("");
  let saveError = $state("");

  let windowSeconds = $state(DEFAULT_WINDOW_S);
  let watching = $state(false);
  let reading = $state(false);
  let rounds = $state(0);
  let roundsNote = $state("");
  let readError = $state("");
  let messages = $state([]);
  let dropped = $state(0);
  let attempted = $state(false);

  // A session generation counter. Stopping (or leaving the page) bumps it, and
  // an in-flight read that comes back afterwards is discarded instead of
  // re-arming the loop the operator just turned off.
  let generation = 0;

  const statusRows = $derived(platformStatusRows(readiness, live));
  const rows = $derived(timelineRows(messages));
  const counts = $derived(countRows(messages));
  const progress = $derived(roundsLabel(rounds));
  const verdict = $derived(
    feedVerdict({ readiness, live, messages, watching, rounds, attempted })
  );

  onMount(loadReadiness);
  onDestroy(() => {
    generation += 1;
    watching = false;
  });

  function applyReadiness(payload) {
    readiness = payload;
    twitchText = channelsToText(channelsOf(payload, "twitch"));
    kickText = channelsToText(channelsOf(payload, "kick"));
    videoId = channelsOf(payload, "youtube")[0] ?? "";
  }

  function channelsOf(payload, platform) {
    const row = (payload?.platforms ?? []).find((item) => item?.platform === platform);
    return row?.channels ?? [];
  }

  async function loadReadiness() {
    readinessError = "";
    try {
      applyReadiness(await api.chatReadiness());
    } catch (cause) {
      readinessError = errorMessage(cause);
    }
  }

  async function saveChannels() {
    // A read in flight belongs to the channel set being replaced, so end it
    // first: its reply must not land in a feed that now describes other
    // channels.
    generation += 1;
    watching = false;
    reading = false;
    saving = true;
    saveNotice = "";
    saveError = "";
    try {
      const saved = await api.setChatChannels(
        channelPayload({ twitch: twitchText, kick: kickText, youtubeVideoId: videoId })
      );
      applyReadiness(saved);
      // The buffer described the old channel set. Keeping it would show
      // messages from a channel the panel no longer watches, next to the new
      // configuration — so it is cleared explicitly, and the notice says so.
      const cleared = messages.length;
      messages = [];
      dropped = 0;
      rounds = 0;
      attempted = false;
      roundsNote = "";
      live = null;
      saveNotice = `Saved ${(saved?.applied ?? []).join(", ")} to this machine's config.`;
      if (cleared > 0) {
        saveNotice += ` Cleared ${cleared} ${cleared === 1 ? "row" : "rows"} read from the previous channel set.`;
      }
    } catch (cause) {
      saveError = errorMessage(cause);
    } finally {
      saving = false;
    }
  }

  async function startWatching() {
    if (watching) return;
    generation += 1;
    const session = generation;
    watching = true;
    rounds = 0;
    attempted = true;
    roundsNote = "";
    readError = "";
    while (watching && rounds < MAX_ROUNDS && session === generation) {
      reading = true;
      try {
        const payload = await api.chatTimeline(windowSeconds);
        if (session !== generation) return;
        live = payload;
        const merged = mergeMessages(messages, payload.messages ?? []);
        messages = merged.messages;
        dropped += merged.dropped;
        rounds += 1;
        roundsNote = `Round ${rounds}: ${merged.added} new ${
          merged.added === 1 ? "message" : "messages"
        }.`;
      } catch (cause) {
        if (session !== generation) return;
        readError = errorMessage(cause);
        break;
      } finally {
        reading = false;
      }
    }
    if (session !== generation) return;
    watching = false;
    if (rounds >= MAX_ROUNDS) {
      roundsNote = `Stopped after ${MAX_ROUNDS} reads (${roundsNote}) — press Start watching to continue.`;
    }
  }

  function stopWatching() {
    generation += 1;
    watching = false;
    reading = false;
    roundsNote = "Stopped. The messages already read stay on screen.";
  }

  // StatusBadge speaks the app's token vocabulary; these are the only two
  // states this page can put it in, and the label stays the engine's status.
  function badgeStatus(row) {
    if (row.code || row.state === "unavailable") return "danger";
    if (row.state === "read") return "healthy";
    if (row.state === "ready") return "connected";
    return "unknown";
  }
</script>

<div class="chat" style={`--chat-target:${CHAT_TARGET_PX}px`}>
  <header class="xpst-page-header">
    <div>
      <h1>Live chat</h1>
      <p>
        Twitch, YouTube and Kick chat in one ordered feed. xPST only reads: it cannot
        post, moderate, delete or reply from this screen.
      </p>
    </div>
    <span class="chat__readonly">Read-only</span>
  </header>

  {#if readinessError}
    <p class="chat__error" role="alert">{readinessError}</p>
  {/if}

  <Card
    title="Platforms"
    description="What xPST can read right now, and what stands in the way. A platform with no channel is not the same as one that is refusing."
  >
    {#each statusRows as row (row.platform)}
      <div class="chat__status" data-platform={row.platform} data-state={row.state}>
        <div class="chat__status-head">
          <PlatformBadge platform={row.platform} />
          <StatusBadge status={badgeStatus(row)} label={row.stateLabel} />
        </div>
        {#if channelLine(row)}
          <p class="chat__status-line">{channelLine(row)}</p>
        {/if}
        {#if row.state === "read" || row.state === "empty"}
          <p class="chat__status-line">
            {row.messages} {row.messages === 1 ? "message" : "messages"} in the last window.
          </p>
        {/if}
        {#if row.code}
          <p class="chat__status-line">Code: <code class="chat__code">{row.code}</code></p>
        {/if}
        {#if row.message}
          <p class="chat__status-line">{row.message}</p>
        {/if}
        {#if row.action && !row.ready}
          <p class="chat__status-line chat__action">Next: {row.action}</p>
        {/if}
        {#if row.note}
          <p class="chat__status-line chat__action">{row.note}</p>
        {/if}
      </div>
    {:else}
      <p class="chat__status-line">Loading the platform verdict from the engine…</p>
    {/each}
  </Card>

  <Card
    title="Channels"
    description="Names only — no credential is stored here. Twitch and Kick chat is public, and YouTube reuses the account xPST already holds."
  >
    <div class="xpst-field">
      <label class="xpst-field__label" for="chat-twitch">Twitch channels</label>
      <input
        id="chat-twitch"
        class="xpst-field__input chat__target"
        bind:value={twitchText}
        placeholder="channel_one, channel_two"
        autocomplete="off"
        autocapitalize="off"
        spellcheck="false"
      />
      <p class="xpst-field__hint">Comma-separated logins. Read anonymously; no Twitch account is needed.</p>
    </div>

    <div class="xpst-field">
      <label class="xpst-field__label" for="chat-kick">Kick channels</label>
      <input
        id="chat-kick"
        class="xpst-field__input chat__target"
        bind:value={kickText}
        placeholder="channel_slug"
        autocomplete="off"
        autocapitalize="off"
        spellcheck="false"
      />
      <p class="xpst-field__hint">Comma-separated slugs. Kick's public chatroom needs no account either.</p>
    </div>

    <div class="xpst-field">
      <label class="xpst-field__label" for="chat-youtube">YouTube live video</label>
      <input
        id="chat-youtube"
        class="xpst-field__input chat__target"
        bind:value={videoId}
        placeholder="video id or watch URL"
        autocomplete="off"
        autocapitalize="off"
        spellcheck="false"
      />
      <p class="xpst-field__hint">
        A broadcast that is live right now. YouTube chat is only exposed while the video is live;
        the URL is reduced to its video id on save.
      </p>
    </div>

    <button
      class="xpst-button chat__target"
      type="button"
      onclick={saveChannels}
      disabled={saving}
    >
      {saving ? "Saving…" : "Save channels"}
    </button>
    <p class="xpst-field__hint">
      Writes chat.twitch_channels / chat.kick_channels / chat.youtube_video_id through the engine's
      config file — the same values the CLI and MCP read.
    </p>
    {#if saveNotice}<p class="chat__notice" role="status">{saveNotice}</p>{/if}
    {#if saveError}<p class="chat__error" role="alert">{saveError}</p>{/if}
  </Card>

  <Card
    title="Watch"
    description="One press opens one bounded read window. A session stops itself after the round ceiling; nothing runs in the background after that."
  >
    <div class="chat__controls">
      <div class="xpst-field">
        <label class="xpst-field__label" for="chat-window">Window</label>
        <select id="chat-window" class="xpst-field__input chat__target" bind:value={windowSeconds} disabled={watching}>
          {#each WINDOW_CHOICES_S as seconds (seconds)}
            <option value={seconds}>{seconds} seconds</option>
          {/each}
        </select>
      </div>
      <button
        class="xpst-button chat__target"
        type="button"
        onclick={startWatching}
        disabled={watching || reading}
      >
        {reading ? "Reading…" : "Start watching"}
      </button>
      <button
        class="xpst-button chat__target"
        data-variant="secondary"
        type="button"
        onclick={stopWatching}
        disabled={!watching}
      >
        Stop
      </button>
    </div>
    <p class="xpst-field__hint">
      Round {progress}. YouTube costs 5 quota units per read; Twitch and Kick cost requests only.
    </p>
    {#if roundsNote}<p class="chat__notice" role="status">{roundsNote}</p>{/if}
    {#if readError}<p class="chat__error" role="alert">{readError}</p>{/if}
  </Card>

  <Card
    title="Merged timeline"
    description="Every message read in this session, oldest first. One row per message: time, platform, author, text."
  >
    {#if counts.length}
      <p class="chat__counts">
        <span class="chat__count">On screen: {rows.length}</span>
        {#each counts as row (row.platform)}
          <span class="chat__count">{row.label}: {row.count}</span>
        {/each}
      </p>
    {/if}

    {#if rows.length}
      <ol class="chat__feed" aria-label="Merged live chat, oldest first" aria-live="polite">
        {#each rows as row (row.key)}
          <li class="chat__row" data-platform={row.platform}>
            <span class="chat__meta">
              <span class="chat__time">{row.time}</span>
              <PlatformBadge platform={row.platform} size={14} />
            </span>
            <span class="chat__line">
              <span class="chat__author">{row.author}</span>
              <span class="chat__text">{row.text}</span>
            </span>
          </li>
        {/each}
      </ol>
      {#if dropped > 0}
        <p class="xpst-field__hint">
          {dropped} oldest {dropped === 1 ? "row was" : "rows were"} dropped to keep this view at
          {BUFFER_LIMIT}.
        </p>
      {/if}
    {:else}
      <div class="chat__empty" data-kind={verdict.kind} role="status">
        <p class="chat__empty-title">{verdict.title}</p>
        <p class="chat__status-line">{verdict.detail}</p>
        {#if verdict.action}<p class="chat__status-line chat__action">Next: {verdict.action}</p>{/if}
      </div>
    {/if}
  </Card>
</div>

<style>
  /* Measured at 390 px: nothing may scroll sideways and every control keeps the
     44 px phone target. Message rows are not controls, but they are the thing a
     thumb scrolls past, so they get the same floor. */
  .chat {
    display: grid;
    gap: var(--xpst-space-4);
    max-width: 760px;
    margin-inline: auto;
    overflow-x: hidden;
  }

  .chat__target {
    min-height: var(--chat-target);
  }

  .chat__readonly {
    align-self: start;
    padding: 2px var(--xpst-space-2);
    border: 1px solid var(--xpst-color-border);
    border-radius: var(--xpst-radius-sm);
    color: var(--xpst-color-text-secondary);
    font: var(--xpst-type-caption);
  }

  .chat__status {
    display: grid;
    gap: var(--xpst-space-1);
    padding-block: var(--xpst-space-2);
    border-bottom: 1px solid var(--xpst-color-border);
  }

  .chat__status:last-of-type {
    border-bottom: 0;
  }

  .chat__status-head {
    display: flex;
    flex-wrap: wrap;
    gap: var(--xpst-space-2);
    align-items: center;
  }

  .chat__status-line {
    margin: 0;
    color: var(--xpst-color-text-secondary);
    overflow-wrap: anywhere;
  }

  .chat__action {
    color: var(--xpst-color-text-primary);
  }

  .chat__code {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    border: 1px solid var(--xpst-color-border);
    border-radius: var(--xpst-radius-sm);
    padding: 0 var(--xpst-space-1);
  }

  .chat__controls {
    display: grid;
    gap: var(--xpst-space-3);
    margin-block-end: var(--xpst-space-2);
  }

  .chat__counts {
    display: flex;
    flex-wrap: wrap;
    gap: var(--xpst-space-3);
    margin: 0 0 var(--xpst-space-2);
    color: var(--xpst-color-text-secondary);
    font: var(--xpst-type-caption);
  }

  .chat__feed {
    display: grid;
    gap: 0;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .chat__row {
    display: grid;
    gap: var(--xpst-space-1);
    align-items: start;
    min-height: var(--chat-target);
    padding-block: var(--xpst-space-2);
    border-bottom: 1px solid var(--xpst-color-border);
    overflow-wrap: anywhere;
  }

  .chat__meta {
    display: flex;
    flex-wrap: wrap;
    gap: var(--xpst-space-2);
    align-items: center;
  }

  .chat__time {
    color: var(--xpst-color-text-secondary);
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.75rem;
  }

  .chat__line {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35em;
    align-items: baseline;
  }

  .chat__author {
    font-weight: 600;
    font-size: 0.9rem;
  }

  .chat__text {
    color: var(--xpst-color-text-primary);
    font: var(--xpst-type-body);
    min-width: 0;
  }

  .chat__empty {
    display: grid;
    gap: var(--xpst-space-1);
    padding-block: var(--xpst-space-3);
  }

  .chat__empty-title {
    margin: 0;
    font-weight: 600;
  }

  .chat__notice {
    margin: var(--xpst-space-2) 0 0;
    color: var(--xpst-color-text-secondary);
  }

  .chat__error {
    margin: var(--xpst-space-2) 0 0;
    color: var(--xpst-color-danger);
  }
</style>
