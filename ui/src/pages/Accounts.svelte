<script>
  import { onMount } from "svelte";
  import { api } from "../lib/api.js";
  import Card from "../lib/components/Card.svelte";
  import EmptyState from "../lib/components/EmptyState.svelte";
  import ErrorState from "../lib/components/ErrorState.svelte";
  import LoadingSkeleton from "../lib/components/LoadingSkeleton.svelte";
  import PlatformBadge from "../lib/components/PlatformBadge.svelte";
  import StatusBadge from "../lib/components/StatusBadge.svelte";

  const FRIENDLY = {
    oauth: "Official OAuth",
    session: "Session sign-in",
    cookies: "Cookie sign-in",
    graph_api: "Official Meta API",
    source_only: "Source only",
    local: "Local folder",
  };

  let state = $state("loading");
  let catalog = $state(null);
  let health = $state(null);
  let error = $state("");

  async function load() {
    state = "loading";
    error = "";
    try {
      const [nextCatalog, nextHealth] = await Promise.all([api.providers(), api.healthStatus()]);
      catalog = nextCatalog;
      health = nextHealth;
      state = "ready";
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
      state = "error";
    }
  }

  onMount(load);
  const providers = $derived(catalog?.providers ?? []);

  function statusFor(provider) {
    if (provider?.state === "disabled") return "disabled";
    if (provider?.state === "ready") return "success";
    if (provider?.state === "blocked_external_review") return "warning";
    return "invalid";
  }

  function statusLabel(provider) {
    const state = provider?.state ?? "unknown";
    return state.replaceAll("_", " ");
  }

  function roleLabel(role) {
    return role.replaceAll("_", " ");
  }
</script>

<header class="xpst-page-header">
  <div>
    <h1>Accounts</h1>
    <p>Capability readiness by role. A source login does not imply video publishing access.</p>
  </div>
  <a class="xpst-button" data-variant="secondary" href="#/settings">Settings</a>
</header>

{#if state === "loading"}
  <LoadingSkeleton rows={7} label="Loading provider capabilities" onRetry={load} />
{:else if state === "error"}
  <ErrorState message={error} retry={load} />
{:else if providers.length === 0}
  <EmptyState title="No provider capabilities reported" description={health?.auth_error || "The local engine has not returned its canonical provider catalog."} actionLabel="Retry" onAction={load} />
{:else}
  <div class="xpst-settings-list" aria-label="Provider capabilities">
    {#each providers as provider (provider.name)}
      <Card>
        <div class="xpst-card__header">
          <div>
            <div class="xpst-inline-meta">
              <PlatformBadge platform={provider.name} />
              <div>
                <h2 class="xpst-card__title">{provider.display_name}</h2>
                <p class="xpst-card__description">{FRIENDLY[provider.auth_mode] ?? provider.auth_mode} · {provider.official_api ? "Official API" : "Provider session"}</p>
              </div>
            </div>
          </div>
          <StatusBadge status={statusFor(provider)} label={statusLabel(provider)} />
        </div>
        <div class="xpst-capability-grid">
          {#each provider.roles as role (role)}
            {@const roleStatus = provider.role_status?.[role]}
            <div class="xpst-capability-row">
              <span>{roleLabel(role)}</span>
              <StatusBadge status={statusFor(roleStatus)} label={statusLabel(roleStatus)} />
            </div>
          {/each}
        </div>
        {#if provider.state === "blocked_external_review"}
          <p class="xpst-card__description">Publishing is blocked by external provider review. Source capability remains separate.</p>
        {:else if provider.state !== "ready" && provider.state !== "disabled"}
          <p class="xpst-card__description">This capability is not ready. Review setup or authentication before attempting a post.</p>
        {/if}
      </Card>
    {/each}
  </div>
{/if}
