<script>
  // The phone flow (docs/PHONE-FIRST-UX.md §2): compose → criteria → preview →
  // confirm → publish → readback, on one 390 px screen.
  //
  // Every step is the engine's object, not a client guess: the upload returns
  // the probe, the plan returns per-destination decisions, the confirm freezes
  // it, the publish returns the engine's own per-destination rows, and the
  // readback says verified only when a platform answered. A step may not be
  // skipped, but going back is always allowed.
  import { onMount } from "svelte";
  import { api, errorMessage } from "../lib/api.js";
  import { getApiToken, setDeviceToken } from "../lib/auth-token.js";
  import {
    PHONE_STEPS,
    PHONE_VIEWPORT_PX,
    MIN_TARGET_PX,
    confirmLabel,
    needsPairing,
    planFromHash,
    platformLabel,
    previewRows,
    probeSummary,
    resumeStep,
    staleReasons,
    uploadIdempotencyKey,
    verificationBadge,
  } from "../lib/phone.js";
  import Card from "../lib/components/Card.svelte";
  import StatusBadge from "../lib/components/StatusBadge.svelte";

  let step = $state("compose");
  let busy = $state(false);
  let notice = $state("");
  let error = $state("");
  let pairing = $state(!getApiToken());

  let pairCode = $state("");
  let pairError = $state("");
  let deviceLabel = $state("");
  let deviceId = $state("");

  let file = $state(null);
  let media = $state(null);
  let mediaError = $state("");
  let caption = $state("");

  let destinations = $state([]);
  let destinationsError = $state("");
  let selected = $state({});

  let plan = $state(null);
  let previewError = $state("");
  let stale = $state([]);
  let result = $state(null);
  let publishError = $state("");
  let readback = $state(null);
  let readbackError = $state("");

  const probe = $derived(probeSummary(media));
  const chosen = $derived(Object.keys(selected).filter((key) => selected[key]));
  const rows = $derived(previewRows(plan?.preview));
  const deferredRows = $derived((readback?.rows ?? []).filter((row) => row.verification === "deferred"));
  const stepNumber = $derived(PHONE_STEPS.findIndex((item) => item.id === step) + 1);

  onMount(loadDestinations);

  // Reopening the app (or opening the same URL on a desktop) resumes the plan
  // the engine already holds: the true state, not a spinner and not a blank
  // compose screen. The plan id lives in the URL so a shared link works.
  onMount(resumePlan);

  function rememberPlan(planId) {
    if (typeof history === "undefined") return;
    const target = planId ? `#/phone?plan=${encodeURIComponent(planId)}` : "#/phone";
    try {
      history.replaceState(null, "", target);
    } catch {
      /* a blocked history API must not break the flow */
    }
  }

  async function resumePlan() {
    const planId = planFromHash(typeof location === "undefined" ? "" : location.hash);
    if (!planId) return;
    busy = true;
    try {
      const loaded = await api.plan(planId);
      plan = loaded;
      media = null;
      caption = loaded.caption ?? "";
      selected = Object.fromEntries((loaded.platforms ?? []).map((name) => [name, true]));
      step = resumeStep(loaded);
      if (step === "readback") await loadReadback(false);
    } catch (cause) {
      // A stale link (plan removed, or another engine) must not trap the phone.
      if (!handleAuth(cause)) notice = `${errorMessage(cause)} Start a new post.`;
    } finally {
      busy = false;
    }
  }

  function handleAuth(cause) {
    if (needsPairing(cause?.cause ?? cause)) {
      pairing = true;
      return true;
    }
    return false;
  }

  async function loadDestinations() {
    destinationsError = "";
    try {
      const payload = await api.providers();
      const rowsIn = payload?.providers ?? payload?.destinations ?? [];
      destinations = rowsIn
        .filter((item) => (item.roles ?? []).includes("video_destination") || item.destination_ready !== undefined)
        .map((item) => ({
          name: item.name,
          label: item.display_name || platformLabel(item.name),
          ready: Boolean(item.destination_ready ?? item.enabled ?? item.can_post),
          note: item.destination_error || (item.enabled ? "" : "Not connected"),
        }));
    } catch (cause) {
      destinationsError = errorMessage(cause);
    }
  }

  async function pairDevice() {
    pairError = "";
    busy = true;
    try {
      const payload = await api.pairDevice(pairCode, deviceLabel || "phone");
      setDeviceToken(payload.token);
      deviceId = payload.device?.device_id ?? "";
      pairing = false;
      notice = payload.message || "This device is paired.";
      await loadDestinations();
    } catch (cause) {
      pairError = errorMessage(cause);
    } finally {
      busy = false;
    }
  }

  async function onFile(event) {
    const picked = event.currentTarget?.files?.[0];
    if (!picked) return;
    file = picked;
    mediaError = "";
    busy = true;
    try {
      const payload = await api.mediaUpload(picked, { idempotencyKey: uploadIdempotencyKey(picked) });
      media = payload.media;
      step = "compose";
    } catch (cause) {
      if (!handleAuth(cause)) mediaError = errorMessage(cause);
    } finally {
      busy = false;
    }
  }

  function toCriteria() {
    if (!media) return;
    step = "criteria";
  }

  async function buildPlan() {
    previewError = "";
    stale = [];
    result = null;
    readback = null;
    busy = true;
    try {
      plan = await api.createPlan({
        media_ids: [media.media_id],
        caption,
        platforms: chosen,
      });
      rememberPlan(plan.plan_id);
      step = "preview";
    } catch (cause) {
      if (!handleAuth(cause)) previewError = errorMessage(cause);
    } finally {
      busy = false;
    }
  }

  async function confirmPlan({ reconfirm = false } = {}) {
    if (!plan) return;
    previewError = "";
    stale = [];
    busy = true;
    try {
      plan = await api.confirmPlan(plan.plan_id, reconfirm ? { confirm_stale: true } : {});
      step = "publish";
    } catch (cause) {
      if (handleAuth(cause)) return;
      const body = cause?.body ?? null;
      if (body?.code === "PLAN_CONFIRM_STALE") {
        stale = staleReasons(body);
        plan = { ...plan, preview: body.preview ?? plan.preview };
      } else {
        previewError = errorMessage(cause);
      }
    } finally {
      busy = false;
    }
  }

  async function publish({ dryRun = false } = {}) {
    if (!plan) return;
    publishError = "";
    busy = true;
    try {
      // A tap on "Publish this post" is the operator's deliberate act, so it
      // carries the same "post now" meaning as the CLI's --now / MCP force:
      // the anti-bot time-of-day window must not silently turn a confirmed post
      // into "deferred until 8am". A rate-limit COOLDOWN still defers (the
      // engine decides that separately) — which is exactly the deferred state
      // M0 requires to be reported, not hidden.
      const payload = await api.publishPlan(plan.plan_id, { dry_run: dryRun, force_now: !dryRun });
      result = payload.result ?? payload.dry_run_result ?? null;
      plan = payload;
      if (dryRun) {
        notice = "Dry run: the plan was checked, nothing was uploaded.";
        step = "preview";
      } else {
        await loadReadback(false);
        step = "readback";
      }
    } catch (cause) {
      if (handleAuth(cause)) return;
      const body = cause?.body ?? null;
      if (body?.result || body?.dry_run_result) {
        result = body.result ?? body.dry_run_result;
        plan = body;
        step = "readback";
        await loadReadback(false);
      } else {
        publishError = errorMessage(cause);
      }
    } finally {
      busy = false;
    }
  }

  async function loadReadback(verify) {
    if (!plan) return;
    readbackError = "";
    busy = true;
    try {
      readback = await api.planReadback(plan.plan_id, verify);
      step = "readback";
    } catch (cause) {
      if (!handleAuth(cause)) readbackError = errorMessage(cause);
    } finally {
      busy = false;
    }
  }

  async function revoke() {
    if (!deviceId) return;
    busy = true;
    try {
      await api.revokeDevice(deviceId);
      setDeviceToken("");
      deviceId = "";
      pairing = true;
      notice = "This device was revoked.";
    } catch (cause) {
      if (!handleAuth(cause)) error = errorMessage(cause);
    } finally {
      busy = false;
    }
  }

  function restart() {
    step = "compose";
    file = null;
    media = null;
    caption = "";
    plan = null;
    result = null;
    readback = null;
    stale = [];
    notice = "";
    previewError = "";
    publishError = "";
    readbackError = "";
    rememberPlan("");
  }
</script>

<div class="phone" style={`--phone-width:${PHONE_VIEWPORT_PX}px; --phone-target:${MIN_TARGET_PX}px`}>
  <header class="xpst-page-header">
    <div>
      <h1>Phone</h1>
      <p>Pick a video, see exactly what xPST will do with it, confirm once, and read back what each platform said.</p>
    </div>
    <span class="phone__step">Step {stepNumber} of {PHONE_STEPS.length}: {PHONE_STEPS[stepNumber - 1]?.label ?? ""}</span>
  </header>

  {#if notice}
    <p class="phone__notice" role="status">{notice}</p>
  {/if}
  {#if error}
    <p class="phone__error" role="alert">{error}</p>
  {/if}

  {#if pairing}
    <Card title="Pair this device" description="Run `xpst pair` on the engine machine, then type the code it prints. The code is single-use and expires.">
      <div class="xpst-field">
        <label class="xpst-field__label" for="pair-code">Pairing code</label>
        <input
          id="pair-code"
          class="xpst-field__input phone__code"
          bind:value={pairCode}
          placeholder="ABCD-2345"
          autocomplete="one-time-code"
          autocapitalize="characters"
        />
      </div>
      <div class="xpst-field">
        <label class="xpst-field__label" for="pair-label">Device label</label>
        <input id="pair-label" class="xpst-field__input" bind:value={deviceLabel} placeholder="phone" />
      </div>
      {#if pairError}<p class="xpst-field__error" role="alert">{pairError}</p>{/if}
      <button class="xpst-button phone__target" type="button" onclick={pairDevice} disabled={busy || !pairCode}>
        {busy ? "Pairing…" : "Pair this device"}
      </button>
    </Card>
  {:else}
    <nav class="phone__steps" aria-label="Publish steps">
      {#each PHONE_STEPS as item (item.id)}
        <span class="phone__step-chip" data-active={item.id === step ? "true" : undefined}>{item.label}</span>
      {/each}
    </nav>

    {#if step === "compose"}
      <Card title="1. Compose" description="Choose the file and write the caption. The engine probes the file before anything else happens.">
        <div class="xpst-field">
          <label class="xpst-field__label" for="phone-file">Video from the camera roll</label>
          <input
            id="phone-file"
            class="xpst-field__input phone__target"
            type="file"
            accept="video/*,image/*"
            onchange={onFile}
          />
          <p class="xpst-field__hint">The bytes go to the engine; the file never has to exist on this machine.</p>
        </div>

        {#if media}
          <dl class="xpst-plan-summary">
            <div>
              <dt>Probed format</dt>
              <dd>{probe.title}</dd>
            </div>
            <div>
              <dt>Container</dt>
              <dd>{probe.detail}</dd>
            </div>
            <div>
              <dt>Media id</dt>
              <dd>{media.media_id}</dd>
            </div>
          </dl>
          {#if !probe.probed}
            <p class="xpst-field__hint" role="status">{probe.detail}</p>
          {/if}
        {/if}
        {#if mediaError}<p class="xpst-field__error" role="alert">{mediaError}</p>{/if}

        <div class="xpst-field">
          <label class="xpst-field__label" for="phone-caption">Caption</label>
          <textarea id="phone-caption" class="xpst-field__input" rows="4" bind:value={caption} placeholder="What is this post?"></textarea>
          <p class="xpst-field__hint">{caption.length} characters</p>
        </div>

        <button class="xpst-button phone__target" type="button" onclick={toCriteria} disabled={busy || !media}>
          Next: destinations
        </button>
      </Card>
    {/if}

    {#if step === "criteria"}
      <Card title="2. Destinations" description="Nothing is implied: only the destinations you tick are in this post.">
        {#if destinationsError}
          <p class="xpst-field__error" role="alert">{destinationsError}</p>
        {:else if !destinations.length}
          <p class="xpst-field__hint">No destinations are configured. Connect an account on the engine first.</p>
        {:else}
          {#each destinations as destination (destination.name)}
            <label class="phone__choice phone__target">
              <input type="checkbox" bind:checked={selected[destination.name]} />
              <span>
                <strong>{destination.label}</strong>
                <span class="xpst-field__hint">{destination.ready ? "Connected" : destination.note || "Not connected"}</span>
              </span>
            </label>
          {/each}
        {/if}
        <div class="xpst-inline-actions">
          <button class="xpst-button" data-variant="secondary" type="button" onclick={() => (step = "compose")}>Back</button>
          <button class="xpst-button phone__target" type="button" onclick={buildPlan} disabled={busy || !chosen.length}>
            {busy ? "Resolving…" : "Preview route"}
          </button>
        </div>
      </Card>
    {/if}

    {#if step === "preview" || step === "confirm"}
      <Card title="3. Route preview" description="A computation, not a write: this is what the engine resolved for this file, caption and destination set.">
        {#if previewError}<p class="xpst-field__error" role="alert">{previewError}</p>{/if}
        {#each rows as row (row.platform)}
          <div class="phone__row">
            <StatusBadge status={row.tone} label={row.label} />
            {#if row.detail}<p class="xpst-field__hint">{row.detail}</p>{/if}
          </div>
        {/each}
        {#if plan?.preview?.blockers?.length}
          <ul class="phone__list">
            {#each plan.preview.blockers as blocker (blocker)}<li>{blocker}</li>{/each}
          </ul>
        {/if}
        {#if plan}
          <dl class="xpst-plan-summary">
            <div><dt>Plan id</dt><dd>{plan.plan_id}</dd></div>
            <div><dt>Content type</dt><dd>{plan.content_type ?? "auto"}</dd></div>
            <div><dt>Caption</dt><dd>{plan.caption || "(empty)"}</dd></div>
          </dl>
        {/if}

        {#if stale.length}
          <p class="xpst-field__error" role="alert">
            This plan changed since the preview: {stale.join("; ")}. Confirm again to publish the new plan.
          </p>
        {/if}

        <div class="xpst-inline-actions">
          <button class="xpst-button" data-variant="secondary" type="button" onclick={() => (step = "criteria")}>Back</button>
          <button
            class="xpst-button phone__target"
            type="button"
            onclick={() => confirmPlan({ reconfirm: stale.length > 0 })}
            disabled={busy || !plan || !rows.some((row) => row.tone === "success")}
          >
            {busy ? "Freezing…" : confirmLabel(plan?.preview)}
          </button>
        </div>
      </Card>
    {/if}

    {#if step === "publish"}
      <Card title="4. Confirm and publish" description="The plan is frozen: media digest, caption, destination set and envelopes. A replay returns the original result.">
        {#each rows as row (row.platform)}
          <div class="phone__row">
            <StatusBadge status={row.tone} label={row.label} />
          </div>
        {/each}
        <p class="xpst-field__hint">
          Posting is paused on this engine until the operator unpauses it. A dry run checks the plan and uploads nothing;
          publishing sends this one confirmed post.
        </p>
        {#if publishError}<p class="xpst-field__error" role="alert">{publishError}</p>{/if}
        <div class="xpst-inline-actions">
          <button class="xpst-button phone__target" type="button" onclick={() => publish({ dryRun: true })} disabled={busy}>
            {busy ? "Checking…" : "Dry run"}
          </button>
          <button class="xpst-button phone__target" data-variant="secondary" type="button" onclick={() => publish({ dryRun: false })} disabled={busy}>
            {busy ? "Publishing…" : "Publish this post"}
          </button>
        </div>
      </Card>
    {/if}

    {#if step === "readback"}
      <Card title="5. Readback" description="What each platform says now. An id nobody read back stays unconfirmed.">
        {#if readbackError}<p class="xpst-field__error" role="alert">{readbackError}</p>{/if}
        {#each readback?.rows ?? [] as row (row.platform)}
          {@const badge = verificationBadge(row)}
          <div class="phone__row">
            <strong>{platformLabel(row.platform)}</strong>
            <StatusBadge status={badge.tone} label={badge.label} />
            {#if row.platform_id}<p class="xpst-field__hint">id {row.platform_id}</p>{/if}
            {#if row.url}<p class="xpst-field__hint"><a class="phone__url" href={row.url} target="_blank" rel="noreferrer">{row.url}</a></p>{/if}
            <p class="xpst-field__hint">{badge.detail}</p>
          </div>
        {/each}
        {#if !readback?.rows?.length}
          <p class="xpst-field__hint">Run a publish (or a dry run) to see per-destination rows.</p>
        {/if}
        <div class="xpst-inline-actions">
          {#if deferredRows.length}
            <button class="xpst-button phone__target" type="button" onclick={() => publish({ dryRun: false })} disabled={busy}>
              {busy ? "Retrying…" : `Retry ${deferredRows.length} deferred`}
            </button>
          {/if}
          <button class="xpst-button phone__target" data-variant="secondary" type="button" onclick={() => loadReadback(true)} disabled={busy || !plan}>
            {busy ? "Checking the platforms…" : "Check the platforms"}
          </button>
          <button class="xpst-button phone__target" data-variant="tertiary" type="button" onclick={restart}>New post</button>
        </div>
      </Card>
    {/if}

    {#if deviceId}
      <p class="xpst-field__hint">
        This browser is paired as {deviceId}.
        <button class="phone__link" type="button" onclick={revoke} disabled={busy}>Revoke</button>
      </p>
    {/if}
  {/if}
</div>

<style>
  /* The acceptance viewport is a 390 px phone: nothing here may scroll
     sideways, and every control is at least a 44 px touch target. */
  .phone {
    display: grid;
    gap: var(--xpst-space-4);
    max-width: var(--phone-width);
    margin-inline: auto;
    overflow-x: hidden;
  }

  .phone__target {
    min-height: var(--phone-target);
  }

  .phone__step {
    color: var(--xpst-color-text-secondary);
    font: var(--xpst-type-caption);
  }

  .phone__steps {
    display: flex;
    flex-wrap: wrap;
    gap: var(--xpst-space-2);
  }

  .phone__step-chip {
    padding: 2px var(--xpst-space-2);
    border: 1px solid var(--xpst-color-border);
    border-radius: var(--xpst-radius-sm);
    color: var(--xpst-color-text-secondary);
    font: var(--xpst-type-caption);
  }

  .phone__step-chip[data-active="true"] {
    border-color: var(--xpst-color-accent, currentColor);
    color: var(--xpst-color-text-primary);
    font-weight: 600;
  }

  .phone__choice {
    display: flex;
    gap: var(--xpst-space-3);
    align-items: center;
    padding: var(--xpst-space-2);
    border: 1px solid var(--xpst-color-border);
    border-radius: var(--xpst-radius-sm);
  }

  .phone__choice input {
    inline-size: 24px;
    block-size: 24px;
  }

  .phone__row {
    display: grid;
    gap: var(--xpst-space-1);
    padding-block: var(--xpst-space-2);
    border-bottom: 1px solid var(--xpst-color-border);
  }

  .phone__list {
    margin: 0;
    padding-inline-start: var(--xpst-space-5);
  }

  .phone__notice {
    margin: 0;
    color: var(--xpst-color-text-secondary);
  }

  .phone__error {
    margin: 0;
    color: var(--xpst-color-danger);
  }

  .phone__code {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .phone__link {
    background: none;
    border: 0;
    padding: 0;
    color: var(--xpst-color-accent, currentColor);
    text-decoration: underline;
    cursor: pointer;
  }

  /* A readback URL is a tap target on a phone: inline text is ~15 px tall, so
     give it the minimum 44 px box without making it look like a button. */
  .phone__url {
    display: inline-flex;
    align-items: center;
    min-height: var(--phone-target);
    overflow-wrap: anywhere;
  }
</style>
