<script>
  import { onMount } from "svelte";
  import { api } from "../lib/api.js";
  import Card from "../lib/components/Card.svelte";
  import EmptyState from "../lib/components/EmptyState.svelte";
  import ErrorState from "../lib/components/ErrorState.svelte";
  import LoadingSkeleton from "../lib/components/LoadingSkeleton.svelte";
  import PlatformBadge from "../lib/components/PlatformBadge.svelte";
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
          { label: "Tracked source posts", value: summary.total_posts ?? 0 },
          { label: "Platform post records", value: summary.total_platform_posts ?? 0 },
          { label: "Posts this week", value: summary.posts_this_week ?? 0 },
          { label: "Top platform", value: summary.best_platform || "—" },
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
  <LoadingSkeleton rows={5} label="Loading dashboard" onRetry={load} />
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

  {#if !hasPosts}
    <section class="xpst-section xpst-section--empty" aria-label="Tracked post status">
      <EmptyState
        title="No posts tracked yet"
        description="The engine has no recorded posts yet. Review account status or return after a post is recorded."
        actionLabel="Review accounts"
        actionHref="#/accounts"
      />
    </section>
  {/if}

  <section class="xpst-section" aria-labelledby="health-heading">
    <div class="xpst-section__heading">
      <h2 id="health-heading">Engine health</h2>
      <StatusBadge status={health?.status ?? "unknown"} />
    </div>
    <Card description={`Engine reports ${health?.total_processed ?? 0} processed item${health?.total_processed === 1 ? "" : "s"}; this is separate from tracked source posts.`}>
      {#if platformHealth.length}
        <div class="xpst-settings-list">
          {#each platformHealth as [name, platform] (name)}
            <div class="xpst-inline-meta" style="justify-content: space-between;">
              <PlatformBadge platform={name} size={16} />
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
{/if}
