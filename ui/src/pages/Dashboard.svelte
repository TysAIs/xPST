<script>
  import { onMount } from "svelte";
  import { api } from "../lib/api.js";
  import { bootFailureMessage, loadWhileEngineStarts } from "../lib/engineStartup.js";
  import Card from "../lib/components/Card.svelte";
  import EmptyState from "../lib/components/EmptyState.svelte";
  import EngineStarting from "../lib/components/EngineStarting.svelte";
  import ErrorState from "../lib/components/ErrorState.svelte";
  import LoadingSkeleton from "../lib/components/LoadingSkeleton.svelte";
  import PlatformBadge from "../lib/components/PlatformBadge.svelte";
  import StatusBadge from "../lib/components/StatusBadge.svelte";
  import { readinessVerdict, roleLabel } from "../lib/labels.js";

  // Home is the landing view, so it is the first thing painted in a launch —
  // measured at BOOT_TO_VISIBLE_SECS=0.165 while the engine only answers at
  // ENGINE_HEALTH_WAIT_SECS=0.690. "starting" is that window: engine coming up,
  // nothing wrong. A real failure still lands on the error card below.
  let state = $state("loading");
  let pendingPolls = 0;
  let summary = $state(null);
  let health = $state(null);
  let error = $state("");

  function fetchHome() {
    return Promise.all([api.summary(), api.healthStatus()]);
  }

  async function load() {
    state = "loading";
    error = "";
    const result = await loadWhileEngineStarts(fetchHome, {
      onWaiting: () => {
        state = "starting";
      },
    });
    if (!result.ok) {
      error = bootFailureMessage(result);
      state = "error";
      return;
    }
    const [nextSummary, nextHealth] = result.value;
    summary = nextSummary;
    health = nextHealth;
    state = "ready";
    // The engine answers immediately while its live probe runs in the
    // background; poll a bounded number of times instead of blocking paint.
    if (nextHealth?.readiness_pending && pendingPolls < 8) {
      pendingPolls += 1;
      setTimeout(load, 1200);
    }
  }

  onMount(load);

  const cards = $derived(
    summary
      ? [
          { label: "Tracked source posts", value: summary.total_posts ?? 0 },
          { label: "Platform post records", value: summary.total_platform_posts ?? 0 },
          { label: "Posts this week", value: summary.posts_this_week ?? 0 },
          { label: "Top platform", value: summary.best_platform || "None yet" },
        ]
      : []
  );
  const platformHealth = $derived(Object.entries(health?.platforms ?? {}));
  const hasPosts = $derived(Number(summary?.total_posts ?? 0) > 0);
  const canCreatePost = $derived(Boolean(health?.can_create_post));
  const readinessPending = $derived(Boolean(health?.readiness_pending || health?.readiness?.pending));
  const readiness = $derived(health?.readiness ?? null);

  // The verdict (pill label + copy) is decided by the ENGINE — the same
  // `readiness` document `/api/health-status` and `/api/onboarding` both serve
  // (src/xpst/readiness.py). Re-deriving it here is how one screen ended up
  // showing three contradictory readiness/health verdicts.
  const verdict = $derived(readinessVerdict(readiness));

  // One row per (platform, role). The role is part of the row's identity: a
  // platform with three roles used to render three identical "Instagram"
  // rows. A row with nothing to say (no state, or a role that is ready) is
  // not rendered at all.
  const readinessRows = $derived(
    (readiness?.blockers ?? []).filter((row) => row && row.platform && row.role && row.state),
  );
  const nextAction = $derived(health?.next_action ?? { kind: "review", label: "Review readiness", role: "video_destination" });
</script>

<header class="xpst-page-header">
  <div>
    <h1>Home</h1>
    <p>One clear view of what is ready, what needs attention, and what xPST has done.</p>
  </div>
  <a class="xpst-button" data-variant="secondary" href={canCreatePost ? "#/compose" : "#/accounts"}>
    {canCreatePost ? "Create post" : nextAction.label}
  </a>
</header>

{#if state === "loading"}
  <LoadingSkeleton rows={5} label="Loading home" onRetry={load} />
{:else if state === "starting"}
  <EngineStarting />
{:else if state === "error"}
  <ErrorState message={error} retry={load} />
{:else}
  <section class="xpst-section" aria-labelledby="readiness-heading">
    <div class="xpst-section__heading">
      <h2 id="readiness-heading">Readiness</h2>
      <StatusBadge status={verdict.status} label={verdict.label} />
    </div>
    <Card description={verdict.detail}>
      {#if readinessPending}
        <p class="xpst-card__description">Live account checks are still running.</p>
      {:else if readinessRows.length}
        <ul class="xpst-settings-list" aria-label="Readiness roles needing attention">
          {#each readinessRows as row (`${row.platform}:${row.role}`)}
            <li class="xpst-inline-meta">
              <PlatformBadge platform={row.platform} size={16} />
              <span class="xpst-inline-meta__role">{row.role_label ?? roleLabel(row.role)}</span>
              <StatusBadge status={row.state} />
              <a class="xpst-inline-link" href="#/accounts">Review</a>
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
        actionHref={canCreatePost ? "#/compose" : "#/accounts"}
      />
    </section>
  {/if}

  <section class="xpst-section" aria-labelledby="health-heading">
    <div class="xpst-section__heading">
      <h2 id="health-heading">Engine health (last recorded)</h2>
      <StatusBadge status={health?.status ?? "unknown"} />
    </div>
    <Card description={`Recorded by the engine's last health run, not a live re-check: ${health?.total_processed ?? 0} processed item${health?.total_processed === 1 ? "" : "s"}. Processing is not the same as a verified post, and live readiness is reported above.`}>
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
