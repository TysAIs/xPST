<script>
  import { onDestroy, onMount } from "svelte";
  import { api, errorMessage } from "../lib/api.js";
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
  // In-app sign-in session driven from this page. phase mirrors the engine's
  // state machine: idle → starting → waiting → (succeeded | cancelled | failed | timed_out).
  let signIn = $state({ phase: "idle", sessionId: "", platform: "", detail: "", error: "", account: null, browserOpened: null, authorizeUrl: "" });
  let pollTimer = null;

  async function load() {
    state = "loading";
    error = "";
    try {
      catalog = await api.providers();
      state = "ready";
      const first = destinationRows(catalog)[0];
      if (!activePlatform && first) activePlatform = first.name;
      await preload(activePlatform);
      if (activePlatform) await verify(activePlatform);
      await adoptLiveSession();
    } catch (cause) {
      error = errorMessage(cause);
      state = "error";
    }
  }

  /**
   * Re-attach to a sign-in the engine is still waiting on.
   *
   * Without this, reloading the app (or navigating away and back) would hide a
   * pending consent flow that is still live in the engine — the user would have
   * no way to see it finish or to cancel it.
   */
  async function adoptLiveSession() {
    const live = await api.signIns().catch(() => null);
    const session = live?.sessions?.[0];
    if (!session) return;
    activePlatform = session.platform;
    signIn = {
      phase: session.phase,
      sessionId: session.session_id,
      platform: session.platform,
      detail: session.detail,
      error: "",
      account: null,
      browserOpened: session.browser_opened,
      authorizeUrl: session.authorize_url,
    };
    schedule(session.poll_after_ms);
  }

  onMount(load);
  onDestroy(stopPolling);

  /**
   * Config-only report (no network) so the page can render the sign-in control
   * immediately. The live probe can take seconds on a machine that has no
   * credential yet — the user must not have to wait for it to see Sign in.
   */
  async function preload(platform) {
    if (!platform) return;
    try {
      const quick = await api.connect(platform, { dry_run: true, verify: false });
      if (activePlatform === platform) report = quick;
    } catch {
      // The live verify below reports the real error; a failed preload is not fatal.
    }
  }

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
      actionError = errorMessage(cause);
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
      actionError = errorMessage(cause);
    } finally {
      enabling = false;
    }
  }

  const connected = $derived(report ? report.connected === true : false);
  const sourceOnly = $derived(report ? report.source_only === true : false);
  const signInSupport = $derived(report?.sign_in ?? null);
  // Sign-in is offered whenever the destination is not verified ready — an
  // authenticated source session (e.g. TikTok cookies) is not a posting
  // credential, so the control must not hide behind it.
  const needsSignIn = $derived(report ? report.state !== "ready" : false);
  const signInActive = $derived(signIn.phase === "starting" || signIn.phase === "waiting" || signIn.phase === "exchanging");

  // ── Sign in ──────────────────────────────────────────────────────

  function stopPolling() {
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  function schedule(ms) {
    stopPolling();
    pollTimer = setTimeout(pollSignIn, ms ?? 1000);
  }

  async function startSignIn(platform) {
    stopPolling();
    signIn = { phase: "starting", sessionId: "", platform, detail: "Opening the consent page…", error: "", account: null, browserOpened: null, authorizeUrl: "" };
    try {
      const envelope = await api.startSignIn(platform, {});
      signIn = {
        phase: envelope.phase,
        sessionId: envelope.session_id,
        platform,
        detail: envelope.detail,
        error: "",
        account: null,
        browserOpened: envelope.browser_opened,
        authorizeUrl: envelope.authorize_url,
      };
      schedule(envelope.poll_after_ms);
    } catch (cause) {
      signIn = { ...signIn, phase: "failed", error: errorMessage(cause) };
    }
  }

  async function pollSignIn() {
    if (!signIn.sessionId) return;
    try {
      const envelope = await api.signInStatus(signIn.sessionId);
      const providerError = envelope.error
        ? `${envelope.error}${envelope.error_description ? ` — ${envelope.error_description}` : ""}`
        : "";
      signIn = {
        ...signIn,
        phase: envelope.phase,
        detail: envelope.detail,
        error: providerError,
        account: envelope.account,
        authorizeUrl: envelope.authorize_url,
      };
      if (envelope.terminal) {
        stopPolling();
        if (envelope.phase === "succeeded") {
          // Connected is never assumed: the engine's live probe decides.
          await verify(signIn.platform);
          catalog = await api.providers();
        }
        return;
      }
      schedule(envelope.poll_after_ms);
    } catch (cause) {
      stopPolling();
      signIn = { ...signIn, phase: "failed", error: errorMessage(cause) };
    }
  }

  async function cancelSignIn() {
    const sessionId = signIn.sessionId;
    stopPolling();
    try {
      const envelope = await api.cancelSignIn(sessionId);
      signIn = { ...signIn, phase: envelope.phase, detail: envelope.detail, error: "" };
    } catch (cause) {
      signIn = { ...signIn, phase: "failed", error: errorMessage(cause) };
    }
  }

  async function selectPlatform(name) {
    if (signInActive) await cancelSignIn();
    await preload(name);
    signIn = { phase: "idle", sessionId: "", platform: "", detail: "", error: "", account: null, browserOpened: null, authorizeUrl: "" };
    await verify(name);
  }
</script>

<header class="xpst-page-header">
  <div>
    <h1>Connect a platform</h1>
    <p>Sign in from here — xPST opens the provider's own consent page and only reports a destination as connected once the engine verified the account.</p>
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
    <Card title="Destinations" description="Pick a platform to sign in, inspect, or enable. Enabling changes only local configuration.">
      <div class="xpst-capability-grid">
        {#each destinations as row (row.name)}
          <button
            class="xpst-create-platform"
            class:is-selected={activePlatform === row.name}
            type="button"
            onclick={() => selectPlatform(row.name)}
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
      {#if verifying && !report}
        <LoadingSkeleton rows={3} label="Verifying account" />
      {:else if actionError && !report}
        <ErrorState title="Verification failed" message={actionError} retry={() => verify(activePlatform)} />
      {:else if report}
        <div class="xpst-section__heading">
          <h2>{report.display_name}</h2>
          <StatusBadge
            status={verifying ? "unknown" : sourceOnly ? "source_only" : connected ? "success" : "error"}
            label={verifying ? "Checking…" : sourceOnly ? "Source only" : connected ? "Connected" : report.state}
          />
        </div>
        {#if actionError}
          <p class="xpst-signin__error">{actionError}</p>
        {/if}
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

        {#if signInActive || signIn.phase !== "idle"}
          <div class="xpst-signin" data-phase={signIn.phase}>
            <div class="xpst-section__heading">
              <strong>
                {#if signIn.phase === "starting"}Starting sign-in…
                {:else if signIn.phase === "waiting"}Waiting for you to approve in {signInSupport?.browser ?? "the browser"}…
                {:else if signIn.phase === "exchanging"}Finishing the exchange…
                {:else if signIn.phase === "succeeded"}Signed in
                {:else if signIn.phase === "cancelled"}Sign-in cancelled
                {:else if signIn.phase === "timed_out"}Sign-in timed out
                {:else}Sign-in failed{/if}
              </strong>
              {#if signInActive}
                <StatusBadge status="warning" label="In progress" />
              {:else if signIn.phase === "succeeded"}
                <StatusBadge status="success" label="Verified" />
              {:else if signIn.phase === "cancelled"}
                <StatusBadge status="disabled" label="Not connected" />
              {:else}
                <StatusBadge status="error" label="Not connected" />
              {/if}
            </div>
            {#if signIn.detail}<p class="xpst-signin__status">{signIn.detail}</p>{/if}
            {#if signIn.error}<p class="xpst-signin__error">{signIn.error}</p>{/if}
            {#if signIn.browserOpened === false && signInActive}
              <p class="xpst-signin__hint">The consent page did not open by itself. Open it here, then approve:</p>
              <a class="xpst-button" data-variant="tertiary" data-size="sm" href={signIn.authorizeUrl} target="_blank" rel="noreferrer noopener">Open the consent page</a>
            {/if}
            {#if signInActive}
              <div class="xpst-inline-actions">
                <button class="xpst-button" data-variant="secondary" type="button" onclick={cancelSignIn}>Cancel sign-in</button>
                <a class="xpst-button" data-variant="tertiary" href={signIn.authorizeUrl} target="_blank" rel="noreferrer noopener">Re-open the consent page</a>
              </div>
              <p class="xpst-signin__hint">Nothing is stored until you approve. Cancelling leaves the account exactly as it is now.</p>
            {:else if signIn.phase === "succeeded"}
              <p class="xpst-signin__hint">Credential stored locally in ~/.xpst (encrypted). No token is ever shown or logged.</p>
            {:else}
              <div class="xpst-inline-actions">
                <button class="xpst-button" type="button" onclick={() => startSignIn(activePlatform)}>Try again</button>
              </div>
            {/if}
          </div>
        {/if}

        <div class="xpst-inline-actions">
          <button class="xpst-button" type="button" onclick={() => verify(activePlatform)} disabled={verifying || signInActive}>Re-check</button>
          {#if needsSignIn && signInSupport?.available && !signInActive}
            <button class="xpst-button" data-variant="secondary" type="button" onclick={() => startSignIn(activePlatform)}>
              Sign in{signInSupport.browser ? ` in ${signInSupport.browser}` : ""}
            </button>
          {/if}
          {#if !report.enabled && !sourceOnly}
            <button class="xpst-button" data-variant="secondary" type="button" onclick={() => enable(activePlatform)} disabled={enabling || verifying || signInActive} aria-busy={enabling ? "true" : undefined}>
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
        {#if signInSupport && !signInSupport.available}
          <p class="xpst-card__description">
            In-app sign-in is not available for this platform yet. {signInSupport.reason}
          </p>
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
        {#if report.sign_in?.available}
          Sign-in runs from this screen: click Sign in and approve in {report.sign_in.browser}. The same flow is available from a terminal as <code>xpst connect {report.platform}</code>.
        {:else}
          This platform cannot be signed in from the app yet — the reason is shown above. From a terminal, <code>xpst connect {report.platform}</code> still works.
        {/if}
      </p>
    </Card>
  {/if}
{/if}
