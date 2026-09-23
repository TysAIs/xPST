<script>
  import { onMount } from "svelte";
  import { api, errorMessage } from "../lib/api.js";
  import { bootFailureMessage, loadWhileEngineStarts } from "../lib/engineStartup.js";
  import { destinationRows, stepRows } from "../lib/firstRun.js";
  import Card from "../lib/components/Card.svelte";
  import EmptyState from "../lib/components/EmptyState.svelte";
  import EngineStarting from "../lib/components/EngineStarting.svelte";
  import ErrorState from "../lib/components/ErrorState.svelte";
  import LoadingSkeleton from "../lib/components/LoadingSkeleton.svelte";
  import StatusBadge from "../lib/components/StatusBadge.svelte";
  import PlatformBadge from "../lib/components/PlatformBadge.svelte";

  let state = $state("loading");
  let error = $state("");
  let onboarding = $state(null);
  let folder = $state("");
  let selected = $state({});
  let saving = $state(false);
  let saveError = $state("");
  let savedNote = $state("");
  let completing = $state(false);

  // A brand-new install lands here (App.svelte opens the wizard on the first
  // run), so this view can be the first thing a launch paints — before the
  // engine sidecar answers. Same starting state as Home, not an error card.
  async function load() {
    state = "loading";
    error = "";
    const result = await loadWhileEngineStarts(() => api.onboarding(), {
      onWaiting: () => {
        state = "starting";
      },
    });
    if (!result.ok) {
      error = bootFailureMessage(result);
      state = "error";
      return;
    }
    const payload = result.value;
    onboarding = payload;
    folder = payload?.source?.path ?? "";
    const next = {};
    for (const row of destinationRows(payload)) next[row.name] = row.enabled;
    selected = next;
    state = "ready";
  }

  onMount(load);

  const steps = $derived(stepRows(onboarding));
  const destinations = $derived(destinationRows(onboarding));
  const readiness = $derived(onboarding?.readiness ?? null);
  const blockingChecks = $derived(readiness?.blocking ?? []);
  const sourceExists = $derived(Boolean(onboarding?.source?.exists));

  function toggleDestination(name) {
    selected = { ...selected, [name]: !selected[name] };
  }

  async function save() {
    saving = true;
    saveError = "";
    savedNote = "";
    try {
      const payload = await api.saveOnboarding({ local: { path: folder }, destinations: selected });
      onboarding = payload;
      savedNote = payload.applied?.length
        ? `Saved: ${payload.applied.join(", ")}`
        : "Nothing changed — the saved values already matched.";
      state = "ready";
    } catch (cause) {
      saveError = errorMessage(cause);
    } finally {
      saving = false;
    }
  }

  async function finish() {
    completing = true;
    saveError = "";
    try {
      await api.completeOnboarding();
      await load();
      location.hash = "#/connect";
    } catch (cause) {
      saveError = errorMessage(cause);
    } finally {
      completing = false;
    }
  }
</script>

<header class="xpst-page-header">
  <div>
    <h1>Set up xPST</h1>
    <p>Pick where your videos live and which platform xPST should publish to. Nothing is uploaded from this screen.</p>
  </div>
  <StatusBadge status={onboarding?.first_run_complete ? "success" : "warning"} label={onboarding?.first_run_complete ? "Setup finished" : "First run"} />
</header>

{#if state === "loading"}
  <LoadingSkeleton rows={5} label="Loading setup state" onRetry={load} />
{:else if state === "starting"}
  <EngineStarting />
{:else if state === "error"}
  <ErrorState title="Could not load setup" message={error} retry={load} />
{:else}
  <section class="xpst-section" aria-labelledby="steps-heading">
    <div class="xpst-section__heading">
      <h2 id="steps-heading">Your setup</h2>
    </div>
    <Card description="Steps marked done are read back from the engine's configuration — not assumed.">
      <ol class="xpst-settings-list" aria-label="Setup steps">
        {#each steps as step (step.id)}
          <li class="xpst-inline-meta" data-step={step.id}>
            <StatusBadge status={step.done ? "success" : step.current ? "warning" : "disabled"} label={step.done ? "Done" : step.current ? "Next" : "Pending"} />
            <span>{step.title}</span>
          </li>
        {/each}
      </ol>
    </Card>
  </section>

  <div class="xpst-create-layout">
    <Card title="Content folder" description="xPST lists local videos from this folder. It is read only when you open Compose.">
      <div class="xpst-field">
        <label class="xpst-field__label" for="onboarding-folder">Folder path</label>
        <input id="onboarding-folder" class="xpst-field__input" bind:value={folder} placeholder="/path/to/your/videos" autocomplete="off" />
      </div>
      <p class="xpst-card__description">
        {#if !folder}
          No folder chosen yet.
        {:else if sourceExists}
          Folder found. Saved videos will be listed in Compose.
        {:else}
          That folder does not exist yet. Save to create the configuration anyway — Compose will report the folder as missing rather than pretending it is fine.
        {/if}
      </p>
    </Card>

    <Card title="Destinations" description="Enabling a destination only marks it as a target. Connecting the account is the next step.">
      {#if destinations.length === 0}
        <EmptyState title="No destinations available" description="The local engine reported no video destination providers." actionLabel="Retry" onAction={load} />
      {:else}
        <div class="xpst-capability-grid">
          {#each destinations as row (row.name)}
            <label class="xpst-create-platform" class:is-selected={selected[row.name]}>
              <input type="checkbox" checked={Boolean(selected[row.name])} onchange={() => toggleDestination(row.name)} />
              <PlatformBadge platform={row.name} size={16} />
              <span>
                <strong>{row.displayName}</strong>
                <small>{row.stateLabel}{row.error ? ` — ${row.error}` : ""}</small>
              </span>
            </label>
          {/each}
        </div>
      {/if}
    </Card>
  </div>

  {#if savedNote}
    <Card description={savedNote}>
      <p class="xpst-card__description">Saved configuration is read back from the engine.</p>
    </Card>
  {/if}

  {#if saveError}
    <ErrorState title="Could not save setup" message={saveError} retry={save} />
  {/if}

  <section class="xpst-section" aria-labelledby="readiness-heading">
    <div class="xpst-section__heading">
      <h2 id="readiness-heading">Readiness</h2>
      <StatusBadge status={readiness?.ready ? "success" : "warning"} label={readiness?.ready ? "Ready" : "Needs attention"} />
    </div>
    <Card description={readiness?.summary ?? "The engine has not reported readiness."}>
      {#if blockingChecks.length}
        <ul aria-label="Setup blockers">
          {#each blockingChecks as check (check.id)}
            <li>{check.label}: {check.message}</li>
          {/each}
        </ul>
      {:else}
        <p class="xpst-card__description">No blocking setup item was reported.</p>
      {/if}
    </Card>
  </section>

  <div class="xpst-inline-actions">
    <button class="xpst-button" type="button" onclick={save} disabled={saving || completing} aria-busy={saving ? "true" : undefined}>
      {saving ? "Saving…" : "Save setup"}
    </button>
    <button class="xpst-button" data-variant="secondary" type="button" onclick={finish} disabled={saving || completing} aria-busy={completing ? "true" : undefined}>
      {completing ? "Finishing…" : "Finish setup and connect a platform"}
    </button>
    <a class="xpst-button" data-variant="secondary" href="#/connect">Skip to Connect</a>
  </div>
{/if}