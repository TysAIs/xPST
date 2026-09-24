<script>
  import { onMount } from "svelte";
  import { api, errorMessage } from "../lib/api.js";
  import { composePostLabel, composePostState, destinationRows, formatBytes, postRequestSummary, targetSummary } from "../lib/firstRun.js";
  import { fileNameOf, mediaItemsFromPaths, mediaKind, mergeMedia, unsupportedPaths } from "../lib/media.js";
  import { installShellDropTarget, pickMediaFile, shellAvailable } from "../lib/native.js";
  import { setLastPost } from "../lib/session.js";
  import Card from "../lib/components/Card.svelte";
  import EmptyState from "../lib/components/EmptyState.svelte";
  import ErrorState from "../lib/components/ErrorState.svelte";
  import LoadingSkeleton from "../lib/components/LoadingSkeleton.svelte";
  import MediaPreview from "../lib/components/MediaPreview.svelte";
  import PlatformBadge from "../lib/components/PlatformBadge.svelte";
  import StatusBadge from "../lib/components/StatusBadge.svelte";

  let mediaState = $state("loading");
  let mediaError = $state("");
  let media = $state(null);
  let folder = $state("");
  let folderInput = $state("");

  // Media added outside the scanned folder: the OS picker and OS drag-and-drop
  // hand the composer absolute paths, never bytes (see lib/native.js).
  let dropped = $state([]);
  let dragging = $state(false);
  let picking = $state(false);
  let pickNotice = $state("");
  const inShell = shellAvailable();

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
      mediaError = errorMessage(cause);
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
      catalogError = errorMessage(cause);
      catalogState = "error";
    }
  }

  async function loadAll() {
    await loadCatalog();
    await loadMedia();
  }

  onMount(() => {
    loadAll();
    // Native drops arrive from the shell (it owns the paths); a plain browser
    // has no path to give, so this is a no-op outside the app window.
    return installShellDropTarget((phase, paths) => {
      if (phase === "enter") {
        dragging = true;
        return;
      }
      if (phase === "leave") {
        dragging = false;
        return;
      }
      dragging = false;
      if (phase === "drop") acceptPaths(paths);
    });
  });

  const destinations = $derived(destinationRows(catalog));
  const items = $derived(mergeMedia(media?.items ?? [], dropped));
  const selectedItem = $derived(
    items.find((item) => item.path === selectedMedia) ??
      (selectedMedia ? { path: selectedMedia, name: fileNameOf(selectedMedia), type: mediaKind(selectedMedia) } : null)
  );
  // Files the engine will refuse are never offered for selection; the reason is
  // shown instead of a silent gap in the list.
  const skippedItems = $derived(media?.skipped ?? []);
  const skippedCount = $derived(media?.skipped_count ?? 0);
  const skipNote = $derived(
    skippedCount
      ? `${skippedCount} file${skippedCount === 1 ? "" : "s"} in this folder cannot be posted: ${skippedItems[0]?.reason ?? "unsupported file type"}`
      : ""
  );
  const chosen = $derived(destinations.filter((row) => selected[row.name] && row.ready));
  const summary = $derived(targetSummary(destinations));
  // The post control is disabled with a visible reason when nothing would be
  // published. The engine refuses the same request with the same error; the UI
  // exists to explain the rule, not to discover it after the fact.
  const postState = $derived(composePostState({ mediaPath: selectedMedia, chosen, busy: posting }));
  const canPost = $derived(postState.canPost);
  const postLabel = $derived(composePostLabel({ dryRun, count: postState.count }));

  function toggleDestination(row) {
    if (!row.ready) return;
    selected = { ...selected, [row.name]: !selected[row.name] };
  }

  /** Adopt paths from the OS picker or a native drop as the current selection. */
  function acceptPaths(paths) {
    const accepted = mediaItemsFromPaths(paths);
    const ignored = unsupportedPaths(paths);
    pickNotice = ignored.length
      ? `${ignored.length} file${ignored.length === 1 ? "" : "s"} ignored — xPST previews and posts video and image files only.`
      : "";
    if (!accepted.length) return;
    dropped = mergeMedia(dropped, accepted);
    selectedMedia = accepted[0].path;
  }

  async function chooseFile() {
    picking = true;
    pickNotice = "";
    const result = await pickMediaFile();
    picking = false;
    if (result.ok) {
      acceptPaths([result.path]);
      return;
    }
    if (result.reason === "cancelled") return;
    if (result.reason === "unavailable") {
      // Honest degradation: never invent a path in a plain browser.
      pickNotice = "The OS file picker is only available in the xPST app window. Open the app, or point the folder field at your media.";
      return;
    }
    pickNotice = `The app refused the file picker: ${result.error ?? "unknown reason"}`;
  }

  function onDomDrop(event) {
    event.preventDefault();
    dragging = false;
    // The shell owns native drops and pushes real paths; a DOM drop in a
    // browser carries no path, and reading the bytes would copy the whole file.
    if (!shellAvailable()) {
      pickNotice = "Dropping a file works in the xPST app window — a browser cannot hand xPST a filesystem path.";
    }
  }

  async function runPreflight() {
    preflighting = true;
    postError = "";
    try {
      preflight = await api.post({
        media_paths: selectedMedia ? [selectedMedia] : [],
        caption,
        platforms: chosen.map((row) => row.name),
        dry_run: true,
      });
    } catch (cause) {
      preflight = null;
      postError = errorMessage(cause);
    } finally {
      preflighting = false;
    }
  }

  async function post() {
    posting = true;
    postError = "";
    const request = postRequestSummary({
      media_paths: selectedMedia ? [selectedMedia] : [],
      caption,
      platforms: chosen.map((row) => row.name),
      dry_run: dryRun,
    });
    try {
      const result = await api.post(request);
      setLastPost({ result, request, error: null });
      location.hash = "#/result";
    } catch (cause) {
      // A refusal or failure is a result too: record the engine's own answer
      // and show it on the result screen instead of guessing here.
      const body = cause?.body ?? null;
      if (body && typeof body === "object") {
        setLastPost({ result: body, request, error: null });
      } else {
        setLastPost({ result: null, request, error: errorMessage(cause) });
      }
      postError = errorMessage(cause);
      location.hash = "#/result";
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

<div class="xpst-create-layout">
  <Card title="Media" description="Local files only. Nothing is downloaded.">
    <div class="xpst-field">
      <label class="xpst-field__label" for="compose-folder">Folder</label>
      <input id="compose-folder" class="xpst-field__input" bind:value={folderInput} placeholder="/path/to/your/videos" autocomplete="off" />
    </div>
    <div class="xpst-inline-actions">
      <button class="xpst-button" data-variant="secondary" type="button" onclick={() => loadMedia(folderInput)} disabled={mediaState === "loading"}>Scan folder</button>
      <button class="xpst-button" data-variant="secondary" type="button" onclick={chooseFile} disabled={picking} aria-busy={picking ? "true" : undefined}>
        {picking ? "Choosing…" : "Choose file…"}
      </button>
    </div>
    <p class="xpst-field__hint">
      {#if inShell}
        Use the OS file picker, or drag a file onto this card.
      {:else}
        Drag-and-drop and the OS file picker are available in the xPST app window; this page reads the folder above.
      {/if}
    </p>
    {#if pickNotice}
      <p class="xpst-field__hint" role="status">{pickNotice}</p>
    {/if}

    <div
      class="xpst-dropzone"
      class:is-active={dragging}
      data-drop-active={dragging ? "true" : "false"}
      ondragover={(event) => {
        event.preventDefault();
        dragging = true;
      }}
      ondragleave={() => (dragging = false)}
      ondrop={onDomDrop}
    >
      {#if selectedItem}
        <MediaPreview item={selectedItem} onClear={() => (selectedMedia = "")} />
      {/if}

      {#if mediaState === "loading"}
        <LoadingSkeleton rows={4} label="Scanning for local videos" onRetry={() => loadMedia(folderInput)} />
      {:else if mediaState === "error"}
        <ErrorState title="Could not read that folder" message={mediaError} retry={() => loadMedia(folderInput)} />
      {:else if items.length === 0}
        <EmptyState
          title={folder ? "No postable files in this folder" : "No content folder yet"}
          description={folder ? (skipNote || `xPST found no files it can post in ${folder}.`) : (media?.hint ?? "Choose a content folder during setup.")}
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
              onclick={() => (selectedMedia = item.path)}
            >
              <span>
                <strong>{item.name}</strong>
                <small>{item.type} · {item.size_bytes ? formatBytes(item.size_bytes) : "added just now"}</small>
              </span>
            </button>
          {/each}
        </div>
        {#if skipNote}
          <p class="xpst-field__hint" role="status">{skipNote}</p>
        {/if}
      {/if}
    </div>
  </Card>

  <Card title="Caption and destinations" description="The caption is sent verbatim. Targets come from the canonical provider catalog.">
    <div class="xpst-field">
      <label class="xpst-field__label" for="compose-caption">Caption</label>
      <textarea id="compose-caption" class="xpst-field__input" rows="4" bind:value={caption} placeholder="Write the caption"></textarea>
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
      {#if destinations.some((row) => row.sourceOnly)}
        <p class="xpst-card__description">
          Source only (never a posting destination): {destinations.filter((row) => row.sourceOnly).map((row) => row.displayName).join(", ")}.
          xPST downloads from {destinations.filter((row) => row.sourceOnly).length === 1 ? "it" : "them"} and cannot post to {destinations.filter((row) => row.sourceOnly).length === 1 ? "it" : "them"}.
        </p>
      {/if}
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
      <button
        class="xpst-button"
        type="button"
        onclick={post}
        disabled={!canPost}
        aria-busy={posting ? "true" : undefined}
        aria-describedby={postState.reason ? "compose-post-blocked-reason" : undefined}
      >
        {posting ? "Posting…" : postLabel}
      </button>
    </div>

    {#if postState.reason}
      <p class="xpst-field__error" id="compose-post-blocked-reason" role="status">
        Posting is disabled: {postState.reason}
      </p>
    {/if}

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