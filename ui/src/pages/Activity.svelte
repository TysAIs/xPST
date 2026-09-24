<script>
  import { onMount } from "svelte";
  import { api, errorMessage } from "../lib/api.js";
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
      error = errorMessage(cause);
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

  let retryingKey = $state("");
  let retryNotice = $state("");
  let retryError = $state("");

  /** Retry one recorded failure through the engine's one-shot recovery. */
  async function retry(failure) {
    const key = `${failure.video_id}:${failure.platform}`;
    if (retryingKey) return;
    retryingKey = key;
    retryError = "";
    retryNotice = "";
    try {
      const payload = await api.retryFailure(failure.video_id, failure.platform, { dry_run: false });
      if (payload?.posted) {
        retryNotice = `Retry succeeded on ${failure.platform}. The post is live.`;
        await load();
      } else {
        retryError = payload?.error?.message || payload?.error_code || `The retry did not post (${payload?.error_code ?? "unknown reason"}).`;
      }
    } catch (cause) {
      retryError = errorMessage(cause);
    } finally {
      retryingKey = "";
    }
  }
</script>

<header class="xpst-page-header">
  <div>
    <h1>Activity &amp; Failures</h1>
    <p>Recorded platform failures from the local state store. Retry sends the post again through the engine's one-shot recovery — it posts only where the destination accepts.</p>
  </div>
</header>

{#if retryError}
  <p class="xpst-field__error" role="status">{retryError}</p>
{:else if retryNotice}
  <p class="xpst-field__hint" role="status">{retryNotice}</p>
{/if}

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
        <thead><tr><th scope="col">Platform</th><th scope="col">Error</th><th scope="col">Video</th><th scope="col">Recovery state</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead>
        <tbody>
          {#each failures as failure (`${failure.video_id}:${failure.platform}`)}
            <tr>
              <td><PlatformBadge platform={failure.platform} /></td>
              <td><strong>{failure.error}</strong><small class="xpst-schedule-path">{failure.last_attempt || "No attempt time"}</small></td>
              <td data-muted="true">{failure.video_id}</td>
              <td><StatusBadge status={retryStatus(failure.retryable)} label={retryLabel(failure.retryable)} /></td>
              <td>
                {#if failure.retryable === true}
                  <button
                    class="xpst-button"
                    data-variant="secondary"
                    type="button"
                    onclick={() => retry(failure)}
                    disabled={retryingKey === `${failure.video_id}:${failure.platform}`}
                    aria-busy={retryingKey === `${failure.video_id}:${failure.platform}` ? "true" : undefined}
                  >
                    {retryingKey === `${failure.video_id}:${failure.platform}` ? "Retrying…" : "Retry"}
                  </button>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  </Card>
{/if}
