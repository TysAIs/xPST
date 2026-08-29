<script>
  import { onMount } from "svelte";
  import { api } from "../lib/api.js";

  let summary = $state(null);
  let error = $state(null);

  onMount(async () => {
    try {
      summary = await api.summary();
    } catch (e) {
      error = e.message;
    }
  });

  const rows = $derived(Object.entries(summary?.engagement_by_platform ?? {}));
</script>

<h1 class="mb-6 text-2xl font-bold tracking-tight">Analytics</h1>

{#if error}
  <p style="color: var(--xpst-danger-text)">Failed to load: {error}</p>
{:else if !summary}
  <p style="color: var(--xpst-text-muted)">Loading…</p>
{:else}
  <div
    class="overflow-hidden rounded-2xl"
    style="background: var(--xpst-surface); box-shadow: var(--xpst-shadow);"
  >
    <table class="w-full text-left text-sm">
      <thead>
        <tr class="border-b" style="border-color: var(--xpst-border); color: var(--xpst-text-muted)">
          <th class="px-4 py-3 font-medium">Platform</th>
          <th class="px-4 py-3 font-medium">Posts</th>
          <th class="px-4 py-3 font-medium">Views</th>
          <th class="px-4 py-3 font-medium">Likes</th>
          <th class="px-4 py-3 font-medium">Comments</th>
          <th class="px-4 py-3 font-medium">Shares</th>
        </tr>
      </thead>
      <tbody>
        {#each rows as [platform, m] (platform)}
          <tr class="border-b last:border-0" style="border-color: var(--xpst-border)">
            <td class="px-4 py-3 font-medium">{platform}</td>
            <td class="px-4 py-3">{m.posts}</td>
            <td class="px-4 py-3">{m.views}</td>
            <td class="px-4 py-3">{m.likes}</td>
            <td class="px-4 py-3">{m.comments}</td>
            <td class="px-4 py-3">{m.shares}</td>
          </tr>
        {/each}
        {#if rows.length === 0}
          <tr><td colspan="6" class="px-4 py-6 text-center" style="color: var(--xpst-text-muted)">
            No engagement snapshots yet.
          </td></tr>
        {/if}
      </tbody>
    </table>
  </div>
  <p class="mt-4 text-xs" style="color: var(--xpst-text-muted)">
    Charts land in Phase 2 — this foundation ships the data plumbing.
  </p>
{/if}
