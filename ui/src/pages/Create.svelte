<script>
  import { onMount } from "svelte";
  import { api } from "../lib/api.js";
  import Card from "../lib/components/Card.svelte";
  import EmptyState from "../lib/components/EmptyState.svelte";
  import ErrorState from "../lib/components/ErrorState.svelte";
  import LoadingSkeleton from "../lib/components/LoadingSkeleton.svelte";
  import StatusBadge from "../lib/components/StatusBadge.svelte";

  let mediaPath = $state("");
  let caption = $state("");
  let platforms = $state(["youtube"]);
  let state = $state("idle");
  let report = $state(null);
  let error = $state("");
  let catalog = $state(null);

  onMount(async () => {
    try {
      catalog = await api.providers();
    } catch {
      catalog = null;
    }
  });

  const destinations = $derived(
    (catalog?.providers ?? []).filter((provider) => provider.roles?.includes("video_destination"))
  );

  async function preflight() {
    state = "loading";
    error = "";
    try {
      report = await api.preflight({ media_path: mediaPath, caption, platforms });
      state = "ready";
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
      state = "error";
    }
  }

  function togglePlatform(name) {
    platforms = platforms.includes(name) ? platforms.filter((item) => item !== name) : [...platforms, name];
  }
</script>

<header class="xpst-page-header">
  <div>
    <h1>Create</h1>
    <p>Run the same local preflight before any future post flow. This surface never uploads media.</p>
  </div>
</header>

<Card title="Post preflight" description="Media, targets, caption limits, and local blockers are checked without network calls.">
  <label class="xpst-form-field">
    <span>Media path</span>
    <input bind:value={mediaPath} placeholder="/path/to/video.mp4" autocomplete="off" />
  </label>
  <label class="xpst-form-field">
    <span>Caption</span>
    <textarea bind:value={caption} rows="4" placeholder="Write the exact caption to validate"></textarea>
  </label>
  <fieldset class="xpst-capability-grid">
    <legend>Destinations</legend>
    {#each destinations as provider (provider.name)}
      <label class="xpst-inline-meta">
        <input type="checkbox" checked={platforms.includes(provider.name)} onchange={() => togglePlatform(provider.name)} />
        {provider.display_name}
      </label>
    {/each}
  </fieldset>
  <button class="xpst-button" type="button" onclick={preflight} disabled={state === "loading"}>Run preflight</button>
</Card>

{#if state === "loading"}
  <LoadingSkeleton rows={4} label="Running local preflight" />
{:else if state === "error"}
  <ErrorState message={error} retry={preflight} />
{:else if state === "ready" && report}
  <Card title="Preflight result" description={`Network calls: ${report.network_calls ? "yes" : "none"}`}>
    <div class="xpst-section__heading">
      <h2>{report.ready ? "Ready for the next step" : "Blocked before posting"}</h2>
      <StatusBadge status={report.ready ? "healthy" : "degraded"} label={report.ready ? "Ready" : "Blocked"} />
    </div>
    {#if report.blockers.length}
      <ul><li>{report.blockers.join("; ")}</li></ul>
    {/if}
    {#if report.warnings.length}
      <p class="xpst-card__description">Warnings: {report.warnings.join("; ")}</p>
    {/if}
  </Card>
{:else if state === "idle"}
  <EmptyState title="Nothing preflighted yet" description="Choose media and targets, then run the local checks." />
{/if}
