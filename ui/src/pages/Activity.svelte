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
  let failures = $state([]);
  let error = $state("");

  async function load() {
    state = "loading";
    error = "";
    try {
      const payload = await api.activity();
      failures = Array.isArray(payload?.failures) ? payload.failures : [];
      state = "ready";
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
      state = "error";
    }
  }

  onMount(load);

  function retryLabel(retryable) {
    return retryable === true ? "Retryable" : retryable === false ? "Terminal" : "Needs review";
  }

  function retryStatus(retryable) {
    return retryable === true ? "warning" : retryable === false ? "error" : "unknown";
  }
</script>

<header class="xpst-page-header">
  <div>
    <h1>Activity &amp; Failures</h1>
    <p>Recorded platform failures from the local state store. No retry or reconcile action runs from this read-only view.</p>
  </div>
</header>

{#if state === "loading"}
  <LoadingSkeleton rows={5} label="Loading activity" onRetry={load} />
{:else if state === "error"}
  <ErrorState message={error} retry={load} />
{:else if failures.length === 0}
  <EmptyState title="No recorded failures" description="The local engine has not recorded a platform failure in its state store." actionLabel="Refresh" onAction={load} />
{:else}
  <Card title="Failures" description={`${failures.length} recorded failure${failures.length === 1 ? "" : "s"}`}>
    <div class="xpst-table-wrap" style="box-shadow: none;">
      <table class="xpst-table">
        <thead><tr><th scope="col">Platform</th><th scope="col">Error</th><th scope="col">Video</th><th scope="col">Recovery state</th></tr></thead>
        <tbody>
          {#each failures as failure (`${failure.video_id}:${failure.platform}`)}
            <tr>
              <td><PlatformBadge platform={failure.platform} /></td>
              <td><strong>{failure.error}</strong><small class="xpst-schedule-path">{failure.last_attempt || "No attempt time"}</small></td>
              <td data-muted="true">{failure.video_id}</td>
              <td><StatusBadge status={retryStatus(failure.retryable)} label={retryLabel(failure.retryable)} /></td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  </Card>
{/if}
