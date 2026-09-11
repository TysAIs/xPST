<script>
  import { onMount } from "svelte";
  import { api } from "../lib/api.js";
  import Card from "../lib/components/Card.svelte";
  import EmptyState from "../lib/components/EmptyState.svelte";
  import ErrorState from "../lib/components/ErrorState.svelte";
  import LoadingSkeleton from "../lib/components/LoadingSkeleton.svelte";

  let state = $state("loading");
  let settings = $state(null);
  let error = $state("");

  async function load() {
    state = "loading";
    error = "";
    try {
      settings = await api.settings();
      state = "ready";
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
      state = "error";
    }
  }

  onMount(() => {
    load();
  });

  const sections = $derived(
    settings
      ? Object.entries(settings).map(([name, value]) => ({ name, value }))
      : []
  );

  const ACCOUNT_LABELS = {
    youtube: "YouTube",
    x: "X",
    instagram: "Instagram",
    tiktok: "TikTok",
    threads: "Threads",
    messenger: "Messenger",
    local: "Local files",
  };

  function humanize(key) {
    return key.replaceAll("_", " ").replace(/(^|\\s)\\S/g, (letter) => letter.toUpperCase());
  }

  function isSensitiveKey(key) {
    return /token|secret|password|cookie|session|credential|hash|key/i.test(key);
  }

  function valueLabel(value) {
    if (value === null || value === undefined || value === "") return "Not configured";
    if (typeof value === "boolean") return value ? "Enabled" : "Disabled";
    if (typeof value === "object") return "Configured";
    return String(value);
  }

  const accountSections = $derived(
    settings?.accounts
      ? Object.entries(settings.accounts).map(([name, value]) => ({ name, value }))
      : []
  );
</script>

<header class="xpst-page-header">
  <div>
    <h1>Settings</h1>
    <p>Review the masked configuration currently visible to the local engine.</p>
  </div>
</header>

{#if state === "loading"}
  <LoadingSkeleton rows={8} label="Loading settings" onRetry={load} />
{:else if state === "error"}
  <ErrorState message={error} retry={load} />
{:else if sections.length === 0}
  <EmptyState
    title="No settings available"
    description="The local engine returned no configuration sections."
    actionLabel="Retry"
    onAction={load}
  />
{:else}
  {#if accountSections.length}
    <section class="xpst-section" aria-labelledby="accounts-heading">
      <div class="xpst-section__heading">
        <h2 id="accounts-heading">Accounts</h2>
        <a class="xpst-button" data-variant="secondary" href="#/accounts">Review capabilities</a>
      </div>
      <div class="xpst-settings-list">
        {#each accountSections as section (section.name)}
          <Card title={ACCOUNT_LABELS[section.name] ?? humanize(section.name)}>
            <dl class="xpst-settings-grid">
              {#each Object.entries(section.value ?? {}).filter(([key]) => !isSensitiveKey(key)) as [key, value] (key)}
                <div><dt>{humanize(key)}</dt><dd>{valueLabel(value)}</dd></div>
              {/each}
            </dl>
          </Card>
        {/each}
      </div>
    </section>
  {/if}

  <section class="xpst-section" aria-labelledby="settings-heading">
    <div class="xpst-section__heading">
      <h2 id="settings-heading">Engine configuration</h2>
      <span class="xpst-card__description">Read-only</span>
    </div>
    <div class="xpst-settings-list">
      {#each sections.filter((section) => section.name !== "accounts") as section (section.name)}
        <Card title={humanize(section.name)}>
          <dl class="xpst-settings-grid">
            {#each Object.entries(section.value ?? {}).filter(([key]) => !isSensitiveKey(key)) as [key, value] (key)}
              <div><dt>{humanize(key)}</dt><dd>{valueLabel(value)}</dd></div>
            {/each}
          </dl>
        </Card>
      {/each}
    </div>
  </section>
  <p class="xpst-card__description">Secrets remain masked server-side. Account capability truth is managed separately from these read-only settings.</p>
{/if}
