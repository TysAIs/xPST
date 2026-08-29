<script>
  import { onMount } from "svelte";
  import { api } from "../lib/api.js";

  let summary = $state(null);
  let health = $state(null);
  let error = $state(null);

  onMount(async () => {
    try {
      [summary, health] = await Promise.all([api.summary(), api.healthStatus()]);
    } catch (e) {
      error = e.message;
    }
  });

  const cards = $derived(
    summary
      ? [
          { label: "Total posts", value: summary.total_posts },
          { label: "Platform posts", value: summary.total_platform_posts },
          { label: "This week", value: summary.posts_this_week },
          { label: "Best platform", value: summary.best_platform },
        ]
      : []
  );

  function statusColor(status) {
    if (status === "ok" || status === "healthy") return "var(--xpst-success-text)";
    if (status === "error" || status === "degraded") return "var(--xpst-danger-text)";
    return "var(--xpst-warning-text)";
  }
</script>

<h1 class="mb-6 text-2xl font-bold tracking-tight">Dashboard</h1>

{#if error}
  <p style="color: var(--xpst-danger-text)">Failed to load: {error}</p>
{:else if !summary}
  <p style="color: var(--xpst-text-muted)">Loading…</p>
{:else}
  <div class="grid grid-cols-2 gap-4 lg:grid-cols-4">
    {#each cards as card (card.label)}
      <div
        class="rounded-2xl p-5"
        style="background: var(--xpst-surface); box-shadow: var(--xpst-shadow);"
      >
        <div class="text-sm" style="color: var(--xpst-text-secondary)">{card.label}</div>
        <div class="mt-1 text-3xl font-semibold tracking-tight">{card.value}</div>
      </div>
    {/each}
  </div>

  <h2 class="mt-8 mb-3 text-lg font-semibold">Engine health</h2>
  <div
    class="rounded-2xl p-5"
    style="background: var(--xpst-surface); box-shadow: var(--xpst-shadow);"
  >
    <div class="mb-3 flex items-center gap-2">
      <span class="font-medium">Overall</span>
      <span class="font-semibold" style="color: {statusColor(health?.status)}">{health?.status ?? "unknown"}</span>
      <span class="text-sm" style="color: var(--xpst-text-muted)">· {health?.total_processed ?? 0} processed</span>
    </div>
    {#each Object.entries(health?.platforms ?? {}) as [name, p] (name)}
      <div class="flex items-center justify-between py-1.5 text-sm">
        <span style="color: var(--xpst-text-secondary)">{name}</span>
        <span style="color: {statusColor(p.status)}">{p.status}</span>
      </div>
    {/each}
  </div>
{/if}
