<script>
  import { onMount } from "svelte";
  import { api } from "../lib/api.js";
  import Card from "../lib/components/Card.svelte";
  import EmptyState from "../lib/components/EmptyState.svelte";
  import ErrorState from "../lib/components/ErrorState.svelte";
  import LoadingSkeleton from "../lib/components/LoadingSkeleton.svelte";

  let state = $state("loading");
  let settings = $state(null);
  let error = $state("");

  async function load() {
    state = "loading";
    error = "";
    try {
      settings = await api.settings();
      state = "ready";
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
      state = "error";
    }
  }

  onMount(() => {
    load();
  });

  const sections = $derived(
    settings
      ? Object.entries(settings).map(([name, value]) => ({ name, value }))
      : []
  );
</script>

<header class="xpst-page-header">
  <div>
    <h1>Settings</h1>
    <p>Review the masked configuration currently visible to the local engine.</p>
  </div>
</header>

{#if state === "loading"}
  <LoadingSkeleton rows={8} label="Loading settings" />
{:else if state === "error"}
  <ErrorState message={error} retry={load} />
{:else if sections.length === 0}
  <EmptyState
    title="No settings available"
    description="The local engine returned no configuration sections."
    actionLabel="Retry"
    onAction={load}
  />
{:else}
  <div class="xpst-settings-list">
    {#each sections as section (section.name)}
      <Card title={section.name}>
        <pre>{JSON.stringify(section.value, null, 2)}</pre>
      </Card>
    {/each}
  </div>
  <p class="xpst-page-header" style="display: block; margin-top: var(--xpst-space-4); margin-bottom: 0; font: var(--xpst-type-caption); color: var(--xpst-color-text-muted);">
    Secrets are masked server-side (the same masker as <code>xpst_config_show</code>). Editing remains outside this foundation.
  </p>
{/if}
