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
    oauth: "Sign in with Google",
    session: "Session sign-in",
    cookies: "Cookie sign-in",
    graph_api: "Sign in with Meta (official)",
    source_only: "Source only (no login)",
    local: "Local folder",
  };

  let state = $state("loading");
  let health = $state(null);
  let error = $state("");

  async function load() {
    state = "loading";
    error = "";
    try {
      health = await api.healthStatus();
      state = "ready";
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
      state = "error";
    }
  }

  onMount(() => {
    load();
  });

  const auth = $derived(health?.auth ?? {});
  const authEntries = $derived(Object.entries(auth));

  function authLabel(mode) {
    return mode ? FRIENDLY[mode] ?? mode : "Not reported";
  }

  function statusFor(entry) {
    if (entry?.error === "disabled") return "disabled";
    return entry?.session_valid ? "success" : "invalid";
  }

  function statusLabel(entry) {
    if (entry?.error === "disabled") return "Disabled";
    return entry?.session_valid ? "Valid" : "Not valid";
  }
</script>

<header class="xpst-page-header">
  <div>
    <h1>Accounts</h1>
    <p>Inspect the authentication state reported by the local engine. This view does not change provider configuration.</p>
  </div>
</header>

{#if state === "loading"}
  <LoadingSkeleton rows={6} label="Loading account status" onRetry={load} />
{:else if state === "error"}
  <ErrorState message={error} retry={load} />
{:else if authEntries.length === 0}
  <EmptyState
    title="No account status available"
    description={health?.auth_error || "The local engine has not returned account information."}
    actionLabel="Retry"
    onAction={load}
  />
{:else}
  <Card title="Connection status" description="Live status is shown as reported; disabled is distinct from invalid.">
    <div class="xpst-table-wrap" style="box-shadow: none;">
      <table class="xpst-table">
        <thead>
          <tr>
            <th scope="col">Platform</th>
            <th scope="col">Sign-in</th>
            <th scope="col">Live session</th>
            <th scope="col">Age (days)</th>
          </tr>
        </thead>
        <tbody>
          {#each authEntries as [platform, entry] (platform)}
            <tr>
              <td><PlatformBadge {platform} /></td>
              <td>{authLabel(entry?.auth_mode)}</td>
              <td><StatusBadge status={statusFor(entry)} label={statusLabel(entry)} /></td>
              <td>{entry?.session_age_days ?? "—"}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  </Card>
{/if}
