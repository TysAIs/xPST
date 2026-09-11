<script>
  import { onMount } from "svelte";
  import { api } from "../lib/api.js";
  import Card from "../lib/components/Card.svelte";
  import EmptyState from "../lib/components/EmptyState.svelte";
  import ErrorState from "../lib/components/ErrorState.svelte";
  import LoadingSkeleton from "../lib/components/LoadingSkeleton.svelte";
  import StatusBadge from "../lib/components/StatusBadge.svelte";

  let state = $state("loading");
  let summary = $state(null);
  let health = $state(null);
  let error = $state("");

  async function load() {
    state = "loading";
    error = "";
    try {
      const [nextSummary, nextHealth] = await Promise.all([api.summary(), api.healthStatus()]);
      summary = nextSummary;
      health = nextHealth;
      state = "ready";
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
      state = "error";
    }
  }

  onMount(() => {
    load();
  });

  const cards = $derived(
    summary
      ? [
          { label: "Total posts", value: summary.total_posts ?? 0 },
          { label: "Platform posts", value: summary.total_platform_posts ?? 0 },
          { label: "This week", value: summary.posts_this_week ?? 0 },
          { label: "Best platform", value: summary.best_platform || "—" },
        ]
      : []
  );

  const platformHealth = $derived(Object.entries(health?.platforms ?? {}));
  const hasPosts = $derived(Number(summary?.total_posts ?? 0) > 0);
</script>

<header class="xpst-page-header">
  <div>
    <h1>Dashboard</h1>
    <p>See what the local engine has processed and which platform needs attention.</p>
  </div>
</header>

{#if state === "loading"}
  <LoadingSkeleton rows={5} label="Loading dashboard" />
{:else if state === "error"}
  <ErrorState message={error} retry={load} />
{:else}
  <div class="xpst-metric-grid" aria-label="Dashboard summary">
    {#each cards as card (card.label)}
      <Card class="xpst-metric-card">
        <span class="xpst-metric-card__label">{card.label}</span>
        <strong class="xpst-metric-card__value">{card.value}</strong>
      </Card>
    {/each}
  </div>

  <section class="xpst-section" aria-labelledby="health-heading">
    <div class="xpst-section__heading">
      <h2 id="health-heading">Engine health</h2>
      <StatusBadge status={health?.status ?? "unknown"} label={health?.status ?? "Unknown"} />
    </div>
    <Card description={`${health?.total_processed ?? 0} processed item${health?.total_processed === 1 ? "" : "s"}`}>
      {#if platformHealth.length}
        <div class="xpst-settings-list">
          {#each platformHealth as [name, platform] (name)}
            <div class="xpst-inline-meta" style="justify-content: space-between;">
              <span>{name}</span>
              <StatusBadge status={platform?.status ?? "unknown"} />
            </div>
          {/each}
        </div>
      {:else}
        <EmptyState
          title="No platform health yet"
          description="The local engine has not reported platform health data."
          actionLabel="Retry"
          onAction={load}
        />
      {/if}
    </Card>
  </section>

  {#if !hasPosts}
    <section class="xpst-section" aria-labelledby="empty-dashboard-heading">
      <EmptyState
        title="No posts tracked yet"
        description="When the engine records a post, its summary and platform health will appear here."
        actionLabel="Review accounts"
        actionHref="#/accounts"
      />
    </section>
  {/if}
{/if}
