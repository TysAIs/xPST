<script>
  import { onMount } from "svelte";
  import { api } from "../lib/api.js";

  let videos = $state(null);
  let error = $state(null);

  onMount(async () => {
    try {
      videos = await api.videos();
    } catch (e) {
      error = e.message;
    }
  });
</script>

<h1 class="mb-6 text-2xl font-bold tracking-tight">Videos</h1>

{#if error}
  <p style="color: var(--xpst-danger-text)">Failed to load: {error}</p>
{:else if !videos}
  <p style="color: var(--xpst-text-muted)">Loading…</p>
{:else}
  <p class="mb-4 text-sm" style="color: var(--xpst-text-secondary)">
    {videos.count} tracked posts across platforms
  </p>
  <div
    class="overflow-hidden rounded-2xl"
    style="background: var(--xpst-surface); box-shadow: var(--xpst-shadow);"
  >
    <table class="w-full text-left text-sm">
      <thead>
        <tr class="border-b" style="border-color: var(--xpst-border); color: var(--xpst-text-muted)">
          <th class="px-4 py-3 font-medium">Platform</th>
          <th class="px-4 py-3 font-medium">Caption</th>
          <th class="px-4 py-3 font-medium">Views</th>
          <th class="px-4 py-3 font-medium">Likes</th>
          <th class="px-4 py-3 font-medium">Comments</th>
        </tr>
      </thead>
      <tbody>
        {#each videos.videos.slice(0, 50) as v, i (i)}
          <tr class="border-b last:border-0" style="border-color: var(--xpst-border)">
            <td class="px-4 py-3 font-medium">{v.platform}</td>
            <td class="max-w-72 truncate px-4 py-3" style="color: var(--xpst-text-secondary)">
              {v.caption || v.post_id}
            </td>
            <td class="px-4 py-3">{v.views ?? 0}</td>
            <td class="px-4 py-3">{v.likes ?? 0}</td>
            <td class="px-4 py-3">{v.comments ?? 0}</td>
          </tr>
        {/each}
        {#if videos.count === 0}
          <tr><td colspan="5" class="px-4 py-6 text-center" style="color: var(--xpst-text-muted)">
            No tracked posts yet.
          </td></tr>
        {/if}
      </tbody>
    </table>
  </div>
{/if}
