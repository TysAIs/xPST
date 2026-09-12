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
  let pendingPolls = 0;
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
      // The engine answers immediately while its live probe runs in the
      // background; poll a bounded number of times instead of blocking paint.
      if (nextHealth?.readiness_pending && pendingPolls < 8) {
        pendingPolls += 1;
        setTimeout(load, 1200);
      }
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
      state = "error";
    }
  }

  onMount(load);

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
  const canCreatePost = $derived(Boolean(health?.can_create_post));
  const readinessPending = $derived(Boolean(health?.readiness_pending));
  const nextAction = $derived(health?.next_action ?? { kind: "review", label: "Review readiness", role: "video_destination" });
  const readinessBlockers = $derived(health?.readiness?.blockers ?? []);
</script>

<header class="xpst-page-header">
  <div>
    <h1>Home</h1>
    <p>One clear view of what is ready, what needs attention, and what xPST has done.</p>
  </div>
  <a class="xpst-button" data-variant="secondary" href={canCreatePost ? "#/create" : "#/accounts"}>
    {canCreatePost ? "Create post" : nextAction.label}
  </a>
</header>

{#if state === "loading"}
  <LoadingSkeleton rows={5} label="Loading home" onRetry={load} />
{:else if state === "error"}
  <ErrorState message={error} retry={load} />
{:else}
  <section class="xpst-section" aria-labelledby="readiness-heading">
    <div class="xpst-section__heading">
      <h2 id="readiness-heading">Readiness</h2>
      <StatusBadge
        status={readinessPending ? "unknown" : health?.readiness?.ready ? "healthy" : "degraded"}
        label={readinessPending ? "Checking…" : health?.readiness?.ready ? "Ready" : "Needs attention"}
      />
    </div>
    <Card description={readinessPending ? "Checking live account readiness — nothing is claimed until the answer is known." : health?.readiness?.ready ? "A destination is available for the next post." : "Posting stays unavailable until the blocker below is resolved."}>
      {#if readinessPending}
        <p class="xpst-card__description">Live account checks are still running.</p>
      {:else if readinessBlockers.length}
        <ul class="xpst-settings-list" aria-label="Readiness blockers">
          {#each readinessBlockers.slice(0, 5) as blocker (`${blocker.platform}:${blocker.role}`)}
            <li class="xpst-inline-meta">
              <PlatformBadge platform={blocker.platform} size={16} />
              <span>{blocker.state.replaceAll("_", " ")}</span>
              <a href="#/accounts">Review</a>
            </li>
          {/each}
        </ul>
      {:else}
        <p class="xpst-card__description">All enabled roles report ready.</p>
      {/if}
    </Card>
  </section>

  <div class="xpst-metric-grid" aria-label="Home summary">
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
        description="The engine has no recorded posts yet. Once a verified post exists, it will appear here."
        actionLabel={canCreatePost ? "Create your first post" : "Review accounts"}
        actionHref={canCreatePost ? "#/create" : "#/accounts"}
      />
    </section>
  {/if}

  <section class="xpst-section" aria-labelledby="health-heading">
    <div class="xpst-section__heading">
      <h2 id="health-heading">Engine health</h2>
      <StatusBadge status={health?.status ?? "unknown"} />
    </div>
    <Card description={`Engine reports ${health?.total_processed ?? 0} processed item${health?.total_processed === 1 ? "" : "s"}; processing is not the same as a verified post.`}>
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
        <EmptyState title="No platform health yet" description="The local engine has not reported platform health data." actionLabel="Retry" onAction={load} />
      {/if}
    </Card>
  </section>
{/if}
