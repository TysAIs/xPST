<script>
  import { onMount } from "svelte";
  import Card from "../lib/components/Card.svelte";

  // No hardcoded fallback: an honest "unknown" beats a version that is always
  // stale. The engine reports the running build's version from its own package
  // metadata (engine_version on /api/health-status).
  let version = $state("unknown");
  let engineStatus = $state(null);

  onMount(async () => {
    try {
      const hs = await fetch("/api/health-status", { headers: { Accept: "application/json" } }).then((res) => res.json());
      version = hs?.engine_version ?? version;
      engineStatus = hs?.status ?? null;
    } catch {
      // About remains useful when the local engine is unavailable.
    }
  });
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
  <Card title="Capability truth" description="Provider readiness is shown separately from this product information.">
    <p class="xpst-card__description">Features blocked by provider review, missing credentials, or external approvals are never represented as ready.</p>
  </Card>
</div>
