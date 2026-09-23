<script>
  import { onMount } from "svelte";
  import { api } from "../lib/api.js";
  import { destinationRows } from "../lib/firstRun.js";
  import Card from "../lib/components/Card.svelte";
  import EmptyState from "../lib/components/EmptyState.svelte";
  import ErrorState from "../lib/components/ErrorState.svelte";
  import LoadingSkeleton from "../lib/components/LoadingSkeleton.svelte";
  import PlatformBadge from "../lib/components/PlatformBadge.svelte";
  import StatusBadge from "../lib/components/StatusBadge.svelte";

  let state = $state("loading");
  let error = $state("");
  let catalog = $state(null);
  let activePlatform = $state("");
  let verifying = $state(false);
  let enabling = $state(false);
  let report = $state(null);
  let actionError = $state("");

  async function load() {
    state = "loading";
    error = "";
    try {
      catalog = await api.providers();
      state = "ready";
      const first = destinationRows(catalog)[0];
      if (!activePlatform && first) activePlatform = first.name;
      if (activePlatform) await verify(activePlatform);
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
      state = "error";
    }
  }

  onMount(load);

  const destinations = $derived(destinationRows(catalog));

  async function verify(platform) {
    if (!platform) return;
    activePlatform = platform;
    verifying = true;
    actionError = "";
    try {
      report = await api.connect(platform, { dry_run: true, verify: true });
    } catch (cause) {
      report = null;
      actionError = cause instanceof Error ? cause.message : String(cause);
    } finally {
      verifying = false;
    }
  }

  async function enable(platform) {
    enabling = true;
    actionError = "";
    try {
      // A real config write, then re-verify — never assumed connected.
      report = await api.connect(platform, { dry_run: false, enable: true, verify: true });
      catalog = await api.providers();
    } catch (cause) {
      actionError = cause instanceof Error ? cause.message : String(cause);
    } finally {
      enabling = false;
    }
  }

  const connected = $derived(report?.connected === true);
  const sourceOnly = $derived(report?.source_only === true);
</script>

<header class="xpst-page-header">
  <div>
    <h1>Connect a platform</h1>
    <p>Check what the engine can actually reach. A destination is only shown as connected when the engine verified the account.</p>
  </div>
  <a class="xpst-button" data-variant="secondary" href="#/onboarding">Setup</a>
</header>

{#if state === "loading"}
  <LoadingSkeleton rows={6} label="Loading provider capabilities" onRetry={load} />
{:else if state === "error"}
  <ErrorState title="Could not load destinations" message={error} retry={load} />
{:else if destinations.length === 0}
  <EmptyState title="No destination platforms reported" description="The local engine returned an empty provider catalog." actionLabel="Retry" onAction={load} />
{:else}
  <div class="xpst-create-layout">
    <Card title="Destinations" description="Pick a platform to inspect or enable. Enabling changes only local configuration.">
      <div class="xpst-capability-grid">
        {#each destinations as row (row.name)}
          <button
            class="xpst-create-platform"
            class:is-selected={activePlatform === row.name}
            type="button"
            onclick={() => verify(row.name)}
            aria-pressed={activePlatform === row.name ? "true" : "false"}
          >
            <PlatformBadge platform={row.name} size={16} />
            <span>
              <strong>{row.displayName}</strong>
              <small>{row.stateLabel}{row.enabled ? "" : " · disabled"} — {row.error || "no live error reported"}</small>
            </span>
            <StatusBadge status={row.status} label={row.ready ? "Ready" : row.stateLabel} />
          </button>
        {/each}
      </div>
    </Card>

    <Card title="Verification" description="Live probe result from the canonical auth check.">
      {#if verifying}
        <LoadingSkeleton rows={3} label="Verifying account" />
      {:else if actionError}
        <ErrorState title="Verification failed" message={actionError} retry={() => verify(activePlatform)} />
      {:else if report}
        <div class="xpst-section__heading">
          <h2>{report.display_name}</h2>
          <StatusBadge
            status={sourceOnly ? "source_only" : connected ? "success" : "error"}
            label={sourceOnly ? "Source only" : connected ? "Connected" : report.state}
          />
        </div>
        {#if sourceOnly}
          <p class="xpst-card__description">
            {report.posting_note || `${report.display_name} is a source only: xPST downloads from it and cannot post to it.`}
          </p>
        {/if}
        <dl class="xpst-plan-summary">
          <div>
            <dt>Destination state</dt>
            <dd>{report.state}</dd>
          </div>
          <div>
            <dt>Enabled in config</dt>
            <dd>{report.enabled ? "Yes" : "No"}</dd>
          </div>
          <div>
            <dt>Live check</dt>
            <dd>{report.live_checked ? "Ran" : "Not run (destination disabled)"}</dd>
          </div>
          <div>
            <dt>{connected || sourceOnly ? "Account" : "Blocker"}</dt>
            <dd>{sourceOnly ? "Used as a download source — not a posting destination." : connected ? "Verified by the engine." : report.error || "The engine reported no reason."}</dd>
          </div>
          <div>
            <dt>Sign-in path</dt>
            <dd>{report.auth_mode}{report.official_api ? " · official API" : " · provider session"}</dd>
          </div>
        </dl>
        <div class="xpst-inline-actions">
          <button class="xpst-button" type="button" onclick={() => verify(activePlatform)} disabled={verifying}>Re-check</button>
          {#if !report.enabled && !sourceOnly}
            <button class="xpst-button" data-variant="secondary" type="button" onclick={() => enable(activePlatform)} disabled={enabling || verifying} aria-busy={enabling ? "true" : undefined}>
              {enabling ? "Enabling…" : "Enable this destination"}
            </button>
          {:else}
            <a class="xpst-button" data-variant="secondary" href="#/compose">Compose a post</a>
          {/if}
          {#if report.docs_url}
            <a class="xpst-button" data-variant="secondary" href={report.docs_url}>Setup docs</a>
          {/if}
        </div>
        {#if report.next_action}
          <p class="xpst-card__description">Next: {report.next_action.label}</p>
        {/if}
      {:else}
        <EmptyState title="No platform checked yet" description="Choose a destination on the left to run a live verification." />
      {/if}
    </Card>
  </div>

  {#if report?.guide?.steps?.length}
    <Card title={`Setup steps for ${report.display_name}`} description={report.guide.why || "Follow these steps, then re-check above."}>
      <ol>
        {#each report.guide.steps as step (step)}
          <li>{step}</li>
        {/each}
      </ol>
      <p class="xpst-card__description">
        Sign-in itself needs a browser or terminal: run <code>xpst connect {report.platform}</code>. This screen never claims an account is connected before the engine verifies it.
      </p>
    </Card>
  {/if}
{/if}