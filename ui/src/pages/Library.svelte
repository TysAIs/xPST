<script>
  import { onMount } from "svelte";
  import { api, errorMessage } from "../lib/api.js";
  import Card from "../lib/components/Card.svelte";
  import EmptyState from "../lib/components/EmptyState.svelte";
  import ErrorState from "../lib/components/ErrorState.svelte";
  import LoadingSkeleton from "../lib/components/LoadingSkeleton.svelte";
  import PlatformBadge from "../lib/components/PlatformBadge.svelte";

  let state = $state("loading");
  let library = $state(null);
  let error = $state("");

  // Per-item delete UI state: which item is working, which succeeded, which refused.
  let deletingId = $state("");
  let deletedId = $state("");
  let deleteError = $state("");
  let confirmId = $state("");

  async function load() {
    state = "loading";
    error = "";
    try {
      library = await api.library();
      state = "ready";
    } catch (cause) {
      error = errorMessage(cause);
      state = "error";
    }
  }

  /** Delete/unpublish every platform result of one library item. Two-step confirm. */
  async function deleteItem(videoId) {
    if (!videoId || deletingId) return;
    deletingId = videoId;
    deleteError = "";
    deletedId = "";
    try {
      const payload = await api.deletePost(videoId);
      const outcomes = (payload?.results ?? []).map((r) => r.outcome);
      if (outcomes.some((o) => o === "deleted" || o === "soft_hidden")) {
        deletedId = videoId;
        confirmId = "";
        await load();
      } else {
        const refused = (payload?.results ?? []).find((r) => r.message);
        deleteError = refused?.message || "The platforms did not confirm the deletion.";
      }
    } catch (cause) {
      deleteError = errorMessage(cause);
    } finally {
      deletingId = "";
    }
  }

  onMount(load);
  const items = $derived(library?.items ?? []);
</script>

<header class="xpst-page-header">
  <div>
    <h1>Library</h1>
    <p>{library?.count ?? 0} local item{library?.count === 1 ? "" : "s"} with verified platform results. Deleting removes the post on each platform — soft-unpublish on YouTube.</p>
  </div>
</header>

{#if deleteError}
  <p class="xpst-field__error" role="status">{deleteError}</p>
{:else if deletedId}
  <p class="xpst-field__hint" role="status">Deleted. The platforms confirmed the removal and the local record keeps a tombstone for analytics.</p>
{/if}

{#if state === "loading"}
  <LoadingSkeleton rows={7} label="Loading library" onRetry={load} />
{:else if state === "error"}
  <ErrorState message={error} retry={load} />
{:else if items.length === 0}
  <EmptyState title="Your library is empty" description="Verified posts will appear here after the engine records them." actionLabel="Review accounts" actionHref="#/accounts" />
{:else}
  <div class="xpst-settings-list" aria-label="Verified library items">
    {#each items as item (item.video_id)}
      <Card title={item.caption || item.video_id} description={`${item.source_platform ?? "Unknown source"} · ${item.source_url ?? "No source path reported"}`}>
        <div class="xpst-inline-meta">
          {#each item.posts as post (`${item.video_id}:${post.platform}`)}
            <a href={post.post_url ?? "#"} target="_blank" rel="noreferrer"><PlatformBadge platform={post.platform} /> {post.post_id ?? "Open verified result"}</a>
          {/each}
        </div>
        {#if confirmId === item.video_id}
          <div class="xpst-inline-meta" role="group" aria-label="Confirm deletion">
            <span>Hard-delete this post from every platform? This cannot be undone.</span>
            <button
              class="xpst-button"
              data-variant="danger"
              type="button"
              onclick={() => deleteItem(item.video_id)}
              disabled={deletingId === item.video_id}
              aria-busy={deletingId === item.video_id ? "true" : undefined}
            >
              {deletingId === item.video_id ? "Deleting…" : "Yes, delete"}
            </button>
            <button class="xpst-button" data-variant="secondary" type="button" onclick={() => (confirmId = "")} disabled={deletingId === item.video_id}>Keep it</button>
          </div>
        {:else}
          <button
            class="xpst-button"
            data-variant="secondary"
            type="button"
            onclick={() => (confirmId = item.video_id)}
            disabled={deletingId === item.video_id}
          >
            Delete post…
          </button>
        {/if}
      </Card>
    {/each}
  </div>
{/if}
