<script>
  import { onMount } from "svelte";
  import { api } from "../lib/api.js";
  import Card from "../lib/components/Card.svelte";
  import EmptyState from "../lib/components/EmptyState.svelte";
  import ErrorState from "../lib/components/ErrorState.svelte";
  import LoadingSkeleton from "../lib/components/LoadingSkeleton.svelte";
  import PlatformBadge from "../lib/components/PlatformBadge.svelte";

  let state = $state("loading");
  let library = $state(null);
  let error = $state("");

  async function load() {
    state = "loading";
    error = "";
    try {
      library = await api.library();
      state = "ready";
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
      state = "error";
    }
  }

  onMount(load);
  const items = $derived(library?.items ?? []);
</script>

<header class="xpst-page-header">
  <div>
    <h1>Library</h1>
    <p>{library?.count ?? 0} local item{library?.count === 1 ? "" : "s"} with verified platform results.</p>
  </div>
</header>

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
      </Card>
    {/each}
  </div>
{/if}
