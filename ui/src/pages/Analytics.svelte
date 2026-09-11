<script>
  import { onMount } from "svelte";
  import { api } from "../lib/api.js";
  import Card from "../lib/components/Card.svelte";
  import EmptyState from "../lib/components/EmptyState.svelte";
  import ErrorState from "../lib/components/ErrorState.svelte";
  import LoadingSkeleton from "../lib/components/LoadingSkeleton.svelte";
  import PlatformBadge from "../lib/components/PlatformBadge.svelte";

  let state = $state("loading");
  let summary = $state(null);
  let error = $state("");

  async function load() {
    state = "loading";
    error = "";
    try {
      summary = await api.summary();
      state = "ready";
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
      state = "error";
    }
  }

  onMount(() => {
    load();
  });

  const rows = $derived(Object.entries(summary?.engagement_by_platform ?? {}));
</script>

<header class="xpst-page-header">
  <div>
    <h1>Analytics</h1>
    <p>Review the metrics already stored by the local engine. No values are fabricated when data is unavailable.</p>
  </div>
</header>

{#if state === "loading"}
  <LoadingSkeleton rows={6} label="Loading analytics" />
{:else if state === "error"}
  <ErrorState message={error} retry={load} />
{:else if rows.length === 0}
  <EmptyState
    title="No engagement snapshots yet"
    description="Analytics will appear after the engine has stored platform metrics."
    actionLabel="Review videos"
    actionHref="#/videos"
  />
{:else}
  <Card title="Engagement by platform" description="Values are sourced from stored snapshots.">
    <div class="xpst-table-wrap" style="box-shadow: none;">
      <table class="xpst-table">
        <thead>
          <tr>
            <th scope="col">Platform</th>
            <th scope="col">Posts</th>
            <th scope="col">Views</th>
            <th scope="col">Likes</th>
            <th scope="col">Comments</th>
            <th scope="col">Shares</th>
          </tr>
        </thead>
        <tbody>
          {#each rows as [platform, metrics] (platform)}
            <tr>
              <td><PlatformBadge {platform} /></td>
              <td>{metrics.posts ?? "—"}</td>
              <td>{metrics.views ?? "—"}</td>
              <td>{metrics.likes ?? "—"}</td>
              <td>{metrics.comments ?? "—"}</td>
              <td>{metrics.shares ?? "—"}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  </Card>
{/if}
