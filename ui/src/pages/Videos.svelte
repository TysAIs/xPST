<script>
  import { onMount } from "svelte";
  import { api } from "../lib/api.js";
  import EmptyState from "../lib/components/EmptyState.svelte";
  import ErrorState from "../lib/components/ErrorState.svelte";
  import LoadingSkeleton from "../lib/components/LoadingSkeleton.svelte";
  import PlatformBadge from "../lib/components/PlatformBadge.svelte";
  import Card from "../lib/components/Card.svelte";

  let state = $state("loading");
  let videos = $state(null);
  let error = $state("");

  async function load() {
    state = "loading";
    error = "";
    try {
      videos = await api.videos();
      state = "ready";
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
      state = "error";
    }
  }

  onMount(() => {
    load();
  });

  const entries = $derived(videos?.videos ?? []);
</script>

<header class="xpst-page-header">
  <div>
    <h1>Videos</h1>
    <p>{videos?.count ?? 0} tracked post{videos?.count === 1 ? "" : "s"} across the configured platforms.</p>
  </div>
</header>

{#if state === "loading"}
  <LoadingSkeleton rows={7} label="Loading videos" />
{:else if state === "error"}
  <ErrorState message={error} retry={load} />
{:else if entries.length === 0}
  <EmptyState
    title="No tracked posts yet"
    description="The local engine will list posts here after it records them."
    actionLabel="Review accounts"
    actionHref="#/accounts"
  />
{:else}
  <Card title="Tracked posts" description="Showing the posts returned by the local engine.">
    <div class="xpst-table-wrap" style="box-shadow: none;">
      <table class="xpst-table">
        <thead>
          <tr>
            <th scope="col">Platform</th>
            <th scope="col">Caption or post ID</th>
            <th scope="col">Views</th>
            <th scope="col">Likes</th>
            <th scope="col">Comments</th>
          </tr>
        </thead>
        <tbody>
          {#each entries.slice(0, 50) as video, index (`${video.video_id ?? ""}:${video.post_id ?? ""}:${index}`)}
            <tr>
              <td><PlatformBadge platform={video.platform} /></td>
              <td data-muted="true">{video.caption || video.post_id || "—"}</td>
              <td>{video.views ?? "—"}</td>
              <td>{video.likes ?? "—"}</td>
              <td>{video.comments ?? "—"}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  </Card>
{/if}
