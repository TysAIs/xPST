<script>
  import { onMount } from "svelte";
  import { api } from "../lib/api.js";

  let settings = $state(null);
  let error = $state(null);

  onMount(async () => {
    try {
      settings = await api.settings();
    } catch (e) {
      error = e.message;
    }
  });

  const sections = $derived(
    settings
      ? Object.entries(settings).map(([name, value]) => ({ name, value }))
      : []
  );
</script>

<h1 class="mb-6 text-2xl font-bold tracking-tight">Settings</h1>

{#if error}
  <p style="color: var(--xpst-danger-text)">Failed to load: {error}</p>
{:else if !settings}
  <p style="color: var(--xpst-text-muted)">Loading…</p>
{:else}
  {#each sections as section (section.name)}
    <div
      class="mb-4 rounded-2xl p-5"
      style="background: var(--xpst-surface); box-shadow: var(--xpst-shadow);"
    >
      <h2 class="mb-3 text-sm font-semibold uppercase tracking-wide" style="color: var(--xpst-text-secondary)">
        {section.name}
      </h2>
      <pre class="overflow-x-auto text-xs leading-relaxed" style="color: var(--xpst-text-secondary)">{JSON.stringify(section.value, null, 2)}</pre>
    </div>
  {/each}
  <p class="text-xs" style="color: var(--xpst-text-muted)">
    Secrets are masked server-side (same masker as xpst_config_show). Editing lands in Phase 2.
  </p>
{/if}
