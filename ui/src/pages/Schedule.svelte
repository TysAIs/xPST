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
  let schedules = $state([]);
  let error = $state("");

  async function load() {
    state = "loading";
    error = "";
    try {
      const payload = await api.schedules();
      schedules = Array.isArray(payload?.schedules) ? payload.schedules : [];
      state = "ready";
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
      state = "error";
    }
  }

  onMount(load);

  function statusFor(status) {
    return { pending: "warning", processing: "warning", completed: "success", failed: "error" }[status] ?? "neutral";
  }

  function formatTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
  }
</script>

<header class="xpst-page-header">
  <div>
    <h1>Schedule</h1>
    <p>Review persisted plans and their truthful state. This view never starts a worker or publishes a post.</p>
  </div>
</header>

{#if state === "loading"}
  <LoadingSkeleton rows={5} label="Loading schedule" onRetry={load} />
{:else if state === "error"}
  <ErrorState message={error} retry={load} />
{:else if schedules.length === 0}
  <EmptyState title="Nothing scheduled" description="Create a schedule from the CLI or MCP. The dashboard will show it here without inventing progress." actionLabel="Refresh" onAction={load} />
{:else}
  <Card title="Scheduled posts" description={`${schedules.length} persisted plan${schedules.length === 1 ? "" : "s"}`}>
    <div class="xpst-table-wrap" style="box-shadow: none;">
      <table class="xpst-table">
        <thead>
          <tr><th scope="col">Content</th><th scope="col">Destinations</th><th scope="col">When</th><th scope="col">State</th></tr>
        </thead>
        <tbody>
          {#each schedules as entry (entry.id)}
            <tr>
              <td>
                <strong>{entry.caption || "Untitled post"}</strong>
                <small class="xpst-schedule-path">{entry.video_path || "No media path"}</small>
              </td>
              <td>
                <div class="xpst-schedule-platforms">
                  {#each entry.platforms ?? [] as platform}
                    <PlatformBadge {platform} size={16} />
                  {:else}
                    <span data-muted="true">All enabled</span>
                  {/each}
                </div>
              </td>
              <td>{formatTime(entry.scheduled_time)}</td>
              <td><StatusBadge status={statusFor(entry.status)} label={entry.status ?? "unknown"} /></td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  </Card>
{/if}
