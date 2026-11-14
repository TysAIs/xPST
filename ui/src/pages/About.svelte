<script>
  import { onMount } from "svelte";
  import Card from "../lib/components/Card.svelte";
  import {
    checkForUpdates,
    describeUpdateResult,
    installUpdate,
    updateNotes,
    updatesAvailable,
  } from "../lib/updates.js";

  // No hardcoded fallback: an honest "unknown" beats a version that is always
  // stale. The engine reports the running build's version from its own package
  // metadata (engine_version on /api/health-status).
  let version = $state("unknown");
  let engineStatus = $state(null);
  let updateState = $state({ status: "idle" });
  let installing = $state(false);
  // False in a plain browser: update checks are a shell capability.
  const canCheck = updatesAvailable();

  onMount(async () => {
    try {
      const hs = await fetch("/api/health-status", { headers: { Accept: "application/json" } }).then((res) => res.json());
      version = hs?.engine_version ?? version;
      engineStatus = hs?.status ?? null;
    } catch {
      // About remains useful when the local engine is unavailable.
    }
    if (canCheck) runCheck();
  });

  async function runCheck() {
    updateState = { status: "checking" };
    updateState = await checkForUpdates();
  }

  async function installNow() {
    installing = true;
    const result = await installUpdate();
    installing = false;
    if (!result.ok) {
      updateState = { status: "error", error: result.error };
    }
  }
</script>

<header class="xpst-page-header">
  <div>
    <h1>About</h1>
    <p>Local-first cross-posting for people and agents, with explicit capability and release truth.</p>
  </div>
</header>

<div class="xpst-settings-list">
  <Card title="xPST" description="Cross-posting control plane">
    <dl class="xpst-about-list">
      <div><dt>Version</dt><dd>{version}</dd></div>
      <div><dt>Engine</dt><dd>{engineStatus ?? "Not connected"}</dd></div>
      <div><dt>Privacy</dt><dd>No telemetry by default; credentials stay local and encrypted.</dd></div>
      <div><dt>License</dt><dd>MIT OR Apache-2.0</dd></div>
    </dl>
  </Card>
  <Card title="Updates" description="Checked once at launch against the signed release manifest.">
    <p class="xpst-card__description">{describeUpdateResult(updateState)}</p>
    {#if updateState.status === "available" && updateNotes(updateState)}
      <pre class="xpst-update-notes">{updateNotes(updateState)}</pre>
    {/if}
    <div class="xpst-update-actions">
      <button type="button" class="xpst-button" data-variant="secondary" onclick={runCheck} disabled={!canCheck || updateState.status === "checking"}>
        Check for updates
      </button>
      {#if updateState.status === "available"}
        <button type="button" class="xpst-button" onclick={installNow} disabled={installing}>
          {installing ? "Installing…" : "Download and install"}
        </button>
      {/if}
    </div>
    {#if !canCheck}
      <p class="xpst-card__description">
        Update checks run inside the xPST desktop app. This page is open in a browser, so the shell
        that verifies and installs an update is not present.
      </p>
    {/if}
    <p class="xpst-card__description">
      The check requests <code>updates/latest.json</code> from the release host and sends no account,
      token or usage data. An update is verified against the release public key before it is installed.
    </p>
  </Card>
  <Card title="Capability truth" description="Provider readiness is shown separately from this product information.">
    <p class="xpst-card__description">Features blocked by provider review, missing credentials, or external approvals are never represented as ready.</p>
  </Card>
</div>
