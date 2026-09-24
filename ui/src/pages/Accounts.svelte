<script>
  import { onMount } from "svelte";
  import { api, errorMessage } from "../lib/api.js";
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
  let refreshing = $state(false);
  let refreshNote = $state("");

  async function load() {
    state = "loading";
    error = "";
    try {
      const [nextCatalog, nextHealth] = await Promise.all([api.providers(), api.healthStatus()]);
      catalog = nextCatalog;
      health = nextHealth;
      state = "ready";
    } catch (cause) {
      error = errorMessage(cause);
      state = "error";
    }
  }

  async function refreshTokens() {
    refreshing = true;
    refreshNote = "";
    try {
      const result = await api.refreshTokens();
      const failed = result?.failed ?? [];
      refreshNote = result?.count
        ? `Refreshed ${result.count - failed.length} of ${result.count} due account(s).${failed.length ? ` Still broken: ${failed.join(", ")}.` : ""}`
        : "No token was due for refresh.";
      await load();
    } catch (cause) {
      refreshNote = errorMessage(cause);
    } finally {
      refreshing = false;
    }
  }

  onMount(load);
  const providers = $derived(catalog?.providers ?? []);
  const badges = $derived(health?.badges ?? {});

  function statusFor(provider) {
    // Source-only (TikTok in source mode) is not "invalid": the download side
    // is wired up and posting is simply not a capability xPST has.
    if (provider?.source_only) return "source_only";
    if (provider?.state === "disabled") return "disabled";
    if (provider?.state === "ready") return "success";
    if (provider?.state === "blocked_external_review") return "warning";
    return "invalid";
  }

  function statusLabel(provider) {
    if (provider?.source_only) return "source only — not a posting destination";
    const state = provider?.state ?? "unknown";
    return state.replaceAll("_", " ");
  }

  function badgeFor(name) {
    const badge = badges[name];
    if (badge) return badge;
    return "unknown";
  }

  function badgeLabel(name) {
    const badge = badgeFor(name);
    return badge.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase());
  }

  function badgeReason(name) {
    const entry = health?.auth?.[name];
    return entry?.badge_reason ?? "";
  }

  function checkedAgo() {
    const iso = health?.auth_checked_at_iso;
    if (!iso) return "";
    const seconds = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
    if (seconds < 90) return `checked ${seconds}s ago`;
    if (seconds < 5400) return `checked ${Math.round(seconds / 60)}m ago`;
    return `checked ${Math.round(seconds / 3600)}h ago`;
  }

  function roleLabel(role) {
    return role.replaceAll("_", " ");
  }
</script>

<header class="xpst-page-header">
  <div>
    <h1>Accounts</h1>
    <p>Capability readiness by role. A source login does not imply video publishing access.</p>
    <p class="xpst-badge-note">
      Badges come from a live check{checkedAgo() ? ` (${checkedAgo()})` : ""} — a stored credential is never shown as connected.
    </p>
  </div>
  <button class="xpst-button" data-variant="secondary" onclick={refreshTokens} disabled={refreshing}>
    {refreshing ? "Refreshing…" : "Refresh accounts"}
  </button>
  <a class="xpst-button" data-variant="secondary" href="#/connect">Connect a platform</a>
  <a class="xpst-button" data-variant="secondary" href="#/settings">Settings</a>
</header>

{#if refreshNote}
  <p class="xpst-badge-note" role="status">{refreshNote}</p>
{/if}

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
        <div class="xpst-inline-meta">
          <StatusBadge status={badgeFor(provider.name)} label={badgeLabel(provider.name)} />
          {#if badgeReason(provider.name)}
            <span class="xpst-badge-note">{badgeReason(provider.name)}</span>
          {/if}
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
