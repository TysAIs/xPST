<script>
  import { onMount } from "svelte";
  import { api } from "../lib/api.js";
  import { destinationRows, formatBytes, postRequestSummary, targetSummary } from "../lib/firstRun.js";
  import {
    draftRequest,
    draftStatusLabel,
    draftStatusTone,
    hasDraftContent,
    isStaleRefusal,
    newestDraft,
    planBanner,
    refusalText,
    restoreDraft,
    selectedPlatforms,
  } from "../lib/drafts.js";
  import { setLastPost } from "../lib/session.js";
  import Card from "../lib/components/Card.svelte";
  import EmptyState from "../lib/components/EmptyState.svelte";
  import ErrorState from "../lib/components/ErrorState.svelte";
  import LoadingSkeleton from "../lib/components/LoadingSkeleton.svelte";
  import PlatformBadge from "../lib/components/PlatformBadge.svelte";
  import StatusBadge from "../lib/components/StatusBadge.svelte";

  let mediaState = $state("loading");
  let mediaError = $state("");
  let media = $state(null);
  let folder = $state("");
  let folderInput = $state("");

  let catalogState = $state("loading");
  let catalogError = $state("");
  let catalog = $state(null);

  let selectedMedia = $state("");
  let caption = $state("");
  let selected = $state({});
  let dryRun = $state(false);
  let posting = $state(false);
  let postError = $state("");
  let preflight = $state(null);
  let preflighting = $state(false);

  // ── Durable draft state ────────────────────────────────────────────
  // The draft is the engine's, not this component's: it is written to disk on
  // every change, so navigating away (or restarting the app) resumes the work
  // instead of losing it. The verdict is the engine's revalidation of what the
  // plan was made against, and a stale plan is never posted silently.
  let draftId = $state("");
  let draftVerdict = $state(null);
  let draftRestored = $state(false);
  let draftNotice = $state("");
  let savingDraft = $state(false);
  let hydrated = $state(false);
  // Set as soon as the user touches anything on this screen: a restore that
  // lands mid-typing must never overwrite what they have already written.
  let userEdited = $state(false);
  let saveTimer = null;

  const banner = $derived(planBanner(draftVerdict));

  async function loadMedia(target = "") {
    mediaState = "loading";
    mediaError = "";
    try {
      const payload = await api.media(target);
      media = payload;
      folder = payload.folder ?? "";
      folderInput = payload.folder ?? target;
      if (!payload.ok) {
        mediaError = payload.error ?? "The folder could not be read.";
        mediaState = "error";
        return;
      }
      mediaState = "ready";
      if (!selectedMedia && payload.items?.length) {
        selectedMedia = payload.items.find((item) => item.type === "video")?.path ?? payload.items[0].path;
      }
    } catch (cause) {
      mediaError = cause instanceof Error ? cause.message : String(cause);
      mediaState = "error";
    }
  }

  async function loadCatalog() {
    catalogState = "loading";
    catalogError = "";
    try {
      catalog = await api.providers();
      const next = {};
      for (const row of destinationRows(catalog)) next[row.name] = row.ready;
      selected = next;
      catalogState = "ready";
    } catch (cause) {
      catalogError = cause instanceof Error ? cause.message : String(cause);
      catalogState = "error";
    }
  }

  /**
   * Restore the newest in-progress draft, then revalidate it against the
   * machine as it is *now* — a plan made before a token expired or a file moved
   * must resume as stale, not as ready.
   */
  async function loadDraft() {
    try {
      const payload = await api.drafts();
      const row = newestDraft(payload);
      if (!row) return;
      const restored = restoreDraft(row);
      draftId = restored.draftId;
      // Anything the user already typed or picked wins over the stored draft —
      // a restore that lands late must never overwrite live work.
      if (!userEdited) {
        if (restored.caption) caption = restored.caption;
        if (restored.mediaPath) selectedMedia = restored.mediaPath;
        selected = { ...selected, ...restored.platforms };
      }
      draftVerdict = restored.verdict;
      draftRestored = true;
      // The list is a moment old; ask again so the resume verdict is fresh.
      try {
        const fresh = await api.draft(draftId);
        draftVerdict = fresh.verdict ?? draftVerdict;
      } catch (cause) {
        if (cause?.status === 404) {
          draftId = "";
          draftNotice = "That draft is no longer stored on disk.";
        }
      }
    } catch (cause) {
      // A draft store that cannot be read must never block composing: the
      // engine's own error is shown and the screen keeps working.
      draftNotice = `Drafts could not be loaded: ${cause instanceof Error ? cause.message : String(cause)}`;
    } finally {
      hydrated = true;
    }
  }

  async function loadAll() {
    await Promise.all([loadCatalog(), loadMedia()]);
    await loadDraft();
  }

  onMount(() => {
    loadAll();
    const flush = () => flushDraft();
    window.addEventListener("pagehide", flush);
    window.addEventListener("beforeunload", flush);
    return () => {
      window.removeEventListener("pagehide", flush);
      window.removeEventListener("beforeunload", flush);
      if (saveTimer) clearTimeout(saveTimer);
    };
  });

  const destinations = $derived(destinationRows(catalog));
  const items = $derived(media?.items ?? []);
  const chosen = $derived(destinations.filter((row) => selected[row.name] && row.ready));
  const summary = $derived(targetSummary(destinations));
  const canPost = $derived(Boolean(selectedMedia) && !posting && !savingDraft);
  const draftLabel = $derived(draftStatusLabel(draftVerdict));

  function currentDraftRequest() {
    return draftRequest({
      draftId,
      mediaPath: selectedMedia,
      caption,
      platforms: selected,
    });
  }

  /** Persist the draft now and adopt the engine's verdict for it. */
  async function saveDraftNow() {
    if (!hasDraftContent({ mediaPath: selectedMedia, caption, platforms: selected })) return;
    savingDraft = true;
    try {
      const payload = await api.saveDraft(currentDraftRequest());
      if (payload?.draft?.id) draftId = payload.draft.id;
      draftVerdict = payload?.verdict ?? draftVerdict;
      if (payload?.recreated) draftNotice = "This draft was recreated from what you had typed.";
    } catch (cause) {
      draftNotice = `Draft not saved: ${cause instanceof Error ? cause.message : String(cause)}`;
    } finally {
      savingDraft = false;
    }
  }

  /** Auto-save on every change, debounced so typing is not a request per key. */
  $effect(() => {
    // Read the values the draft is made of, so any edit re-runs this effect.
    const tracked = [draftId, selectedMedia, caption, JSON.stringify(selected)];
    void tracked;
    if (!hydrated) return;
    if (!hasDraftContent({ mediaPath: selectedMedia, caption, platforms: selected })) return;
    saveTimer = setTimeout(() => {
      saveTimer = null;
      saveDraftNow();
    }, 400);
    return () => {
      if (saveTimer) clearTimeout(saveTimer);
    };
  });

  /** Last-resort flush for a closing window; never blocks navigation. */
  function flushDraft() {
    if (!hasDraftContent({ mediaPath: selectedMedia, caption, platforms: selected })) return;
    const body = JSON.stringify(currentDraftRequest());
    try {
      if (navigator.sendBeacon) {
        navigator.sendBeacon("/api/drafts", new Blob([body], { type: "application/json" }));
        return;
      }
    } catch {
      // fall through to fetch
    }
    fetch("/api/drafts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
      keepalive: true,
    }).catch(() => {
      // A flush that fails has no UI left to report into.
    });
  }

  async function discardDraft() {
    const id = draftId;
    draftId = "";
    draftVerdict = null;
    draftRestored = false;
    draftNotice = "Draft discarded.";
    caption = "";
    selected = {};
    if (id) {
      try {
        await api.deleteDraft(id);
      } catch (cause) {
        draftNotice = `Draft could not be discarded: ${cause instanceof Error ? cause.message : String(cause)}`;
      }
    }
  }

  function toggleDestination(row) {
    if (!row.ready) return;
    userEdited = true;
    selected = { ...selected, [row.name]: !selected[row.name] };
  }

  async function runPreflight() {
    preflighting = true;
    postError = "";
    try {
      // Save first so the plan is recorded against exactly what was checked.
      await saveDraftNow();
      preflight = await api.post({
        media_paths: selectedMedia ? [selectedMedia] : [],
        caption,
        platforms: chosen.map((row) => row.name),
        draft_id: draftId || undefined,
        dry_run: true,
      });
      if (preflight?.draft) draftVerdict = preflight.draft;
    } catch (cause) {
      preflight = null;
      postError = cause instanceof Error ? cause.message : String(cause);
    } finally {
      preflighting = false;
    }
  }

  async function post({ confirmStale = false } = {}) {
    posting = true;
    postError = "";
    await saveDraftNow();
    const request = postRequestSummary({
      media_paths: selectedMedia ? [selectedMedia] : [],
      caption,
      platforms: chosen.map((row) => row.name),
      dry_run: dryRun,
    });
    if (draftId) request.draft_id = draftId;
    if (confirmStale) request.confirm_stale = true;
    try {
      const result = await api.post(request);
      setLastPost({ result, request, error: null });
      if (result?.draft) draftVerdict = result.draft;
      location.hash = "#/result";
    } catch (cause) {
      // A refusal or failure is a result too: record the engine's own answer
      // and show it on the result screen instead of guessing here.
      const body = cause?.body ?? null;
      const staleRefusal = isStaleRefusal(body);
      if (body && typeof body === "object") {
        setLastPost({ result: body, request, error: null });
        if (body.draft) draftVerdict = body.draft;
        postError = refusalText(body);
      } else {
        setLastPost({ result: null, request, error: cause instanceof Error ? cause.message : String(cause) });
        postError = cause instanceof Error ? cause.message : String(cause);
      }
      // A stale refusal leaves the compose screen in place, with the reasons and
      // the re-confirm control on screen — sending the user elsewhere would hide
      // the one action that can resolve it.
      if (!staleRefusal) location.hash = "#/result";
    } finally {
      posting = false;
    }
  }
</script>

<header class="xpst-page-header">
  <div>
    <h1>Compose</h1>
    <p>Pick a video, write the caption, choose destinations, then post. The engine decides whether the post is allowed.</p>
  </div>
  <a class="xpst-button" data-variant="secondary" href="#/result">Last post</a>
</header>

{#if draftId || draftNotice}
  <Card title="Draft" description="Kept on this machine only, and resumed when you come back.">
    <div class="xpst-section__heading">
      <h2>{draftRestored ? "Draft restored" : "Draft"}</h2>
      <StatusBadge status={draftStatusTone(draftVerdict)} label={draftLabel} />
    </div>
    <dl class="xpst-plan-summary">
      <div>
        <dt>Draft id</dt>
        <dd>{draftId || "none"}</dd>
      </div>
      <div>
        <dt>Destinations</dt>
        <dd>{selectedPlatforms(selected).join(", ") || "none"}</dd>
      </div>
      <div>
        <dt>Plan checked</dt>
        <dd>{draftVerdict?.validated_at ?? "not yet"}</dd>
      </div>
    </dl>
    {#if draftNotice}
      <p class="xpst-card__description" role="status">{draftNotice}</p>
    {/if}
    <div class="xpst-inline-actions">
      <button class="xpst-button" data-variant="secondary" type="button" onclick={discardDraft} disabled={posting || savingDraft}>
        Discard draft
      </button>
      <span class="xpst-field__hint">{savingDraft ? "Saving…" : "Saved automatically"}</span>
    </div>
  </Card>
{/if}

{#if banner.show}
  <Card title="Plan" description="A plan is only as current as the machine it was made on.">
    <div class="xpst-section__heading">
      <h2>{banner.title}</h2>
      <StatusBadge status={banner.tone} label={banner.tone === "success" ? "Current" : banner.requiresReconfirmation ? "Stale" : "Check"} />
    </div>
    <p class="xpst-card__description">{banner.detail}</p>
    {#if banner.reasons.length}
      <ul class="xpst-card__description">
        {#each banner.reasons as reason (reason.code + (reason.subject ?? ""))}
          <li>{reason.message}</li>
        {/each}
      </ul>
    {/if}
    {#if banner.requiresReconfirmation}
      <div class="xpst-inline-actions">
        <button class="xpst-button" data-variant="secondary" type="button" onclick={runPreflight} disabled={preflighting || posting}>
          {preflighting ? "Checking…" : "Check again"}
        </button>
        <button class="xpst-button" type="button" onclick={() => post({ confirmStale: true })} disabled={posting || !canPost}>
          {posting ? "Posting…" : "Re-confirm and post"}
        </button>
      </div>
    {/if}
  </Card>
{/if}

<div class="xpst-create-layout">
  <Card title="Video" description="Local files only. Nothing is downloaded.">
    <div class="xpst-field">
      <label class="xpst-field__label" for="compose-folder">Folder</label>
      <input id="compose-folder" class="xpst-field__input" bind:value={folderInput} placeholder="/path/to/your/videos" autocomplete="off" />
    </div>
    <div class="xpst-inline-actions">
      <button class="xpst-button" data-variant="secondary" type="button" onclick={() => loadMedia(folderInput)} disabled={mediaState === "loading"}>Scan folder</button>
    </div>

    {#if mediaState === "loading"}
      <LoadingSkeleton rows={4} label="Scanning for local videos" onRetry={() => loadMedia(folderInput)} />
    {:else if mediaState === "error"}
      <ErrorState title="Could not read that folder" message={mediaError} retry={() => loadMedia(folderInput)} />
    {:else if items.length === 0}
      <EmptyState
        title={folder ? "No videos in this folder" : "No content folder yet"}
        description={folder ? `xPST found no video or image files in ${folder}.` : (media?.hint ?? "Choose a content folder during setup.")}
        actionLabel="Set up the folder"
        actionHref="#/onboarding"
      />
    {:else}
      <div class="xpst-create-platforms" role="radiogroup" aria-label="Choose a video">
        {#each items as item (item.path)}
          <button
            class="xpst-create-platform"
            class:is-selected={selectedMedia === item.path}
            type="button"
            role="radio"
            aria-checked={selectedMedia === item.path ? "true" : "false"}
            onclick={() => {
              userEdited = true;
              selectedMedia = item.path;
            }}
          >
            <span>
              <strong>{item.name}</strong>
              <small>{item.type} · {formatBytes(item.size_bytes)}</small>
            </span>
          </button>
        {/each}
      </div>
    {/if}
  </Card>

  <Card title="Caption and destinations" description="The caption is sent verbatim. Targets come from the canonical provider catalog.">
    <div class="xpst-field">
      <label class="xpst-field__label" for="compose-caption">Caption</label>
      <textarea id="compose-caption" class="xpst-field__input" rows="4" bind:value={caption} oninput={() => (userEdited = true)} placeholder="Write the caption"></textarea>
      <p class="xpst-field__hint">{caption.length} characters</p>
    </div>

    {#if catalogState === "loading"}
      <LoadingSkeleton rows={4} label="Loading destinations" onRetry={loadCatalog} />
    {:else if catalogState === "error"}
      <ErrorState title="Could not load destinations" message={catalogError} retry={loadCatalog} />
    {:else if destinations.length === 0}
      <EmptyState title="No destinations reported" description="The engine returned an empty provider catalog." actionLabel="Retry" onAction={loadCatalog} />
    {:else}
      <fieldset class="xpst-capability-grid">
        <legend>Destinations — {summary.ready} of {summary.total} ready</legend>
        {#each destinations as row (row.name)}
          <label class="xpst-inline-meta">
            <input
              type="checkbox"
              checked={Boolean(selected[row.name])}
              disabled={!row.ready}
              onchange={() => toggleDestination(row)}
            />
            <PlatformBadge platform={row.name} size={16} />
            <span>{row.displayName}</span>
            <StatusBadge status={row.status} label={row.stateLabel} />
          </label>
        {/each}
      </fieldset>
      {#if summary.ready === 0}
        <p class="xpst-card__description">
          No destination is ready, so posting is refused by the engine. <a class="xpst-inline-link" href="#/connect">Connect a destination</a>.
        </p>
      {/if}
    {/if}

    <label class="xpst-inline-meta">
      <input type="checkbox" bind:checked={dryRun} />
      <span>Dry run — plan the post, upload nothing</span>
    </label>

    <div class="xpst-inline-actions">
      <button class="xpst-button" data-variant="secondary" type="button" onclick={runPreflight} disabled={preflighting || posting} aria-busy={preflighting ? "true" : undefined}>
        {preflighting ? "Checking…" : "Check without posting"}
      </button>
      <button class="xpst-button" type="button" onclick={() => post()} disabled={!canPost} aria-busy={posting ? "true" : undefined}>
        {posting ? "Posting…" : dryRun ? "Run dry run" : `Post to ${chosen.length} destination${chosen.length === 1 ? "" : "s"}`}
      </button>
    </div>

    {#if postError}
      <p class="xpst-field__error" role="status">{postError}</p>
    {/if}
  </Card>
</div>

{#if preflight}
  <Card title="Preflight (nothing uploaded)" description={preflight.blockers?.length ? "The engine would refuse this post." : "The engine reports this post can run."}>
    <div class="xpst-section__heading">
      <h2>{preflight.ok ? "Ready to post" : "Blocked before posting"}</h2>
      <StatusBadge status={preflight.ok ? "success" : "error"} label={preflight.ok ? "Ready" : "Blocked"} />
    </div>
    <dl class="xpst-plan-summary">
      <div>
        <dt>Destinations</dt>
        <dd>{preflight.requested?.join(", ") || "none"}</dd>
      </div>
      <div>
        <dt>Blockers</dt>
        <dd>{preflight.blockers?.length ? preflight.blockers.join("; ") : "None"}</dd>
      </div>
      <div>
        <dt>Uploaded</dt>
        <dd>No — this was a plan only.</dd>
      </div>
    </dl>
  </Card>
{/if}