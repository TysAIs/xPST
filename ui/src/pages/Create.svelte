<script>
  import { onMount } from "svelte";
  import Button from "../lib/components/Button.svelte";
  import Card from "../lib/components/Card.svelte";
  import ErrorState from "../lib/components/ErrorState.svelte";
  import FormField from "../lib/components/FormField.svelte";
  import PlatformBadge from "../lib/components/PlatformBadge.svelte";

  const platformOrder = ["youtube", "x", "instagram", "tiktok", "threads"];
  let filePath = $state("");
  let caption = $state("");
  let selected = $state(["youtube", "x", "instagram"]);
  let providers = $state(null);
  let readiness = $state(null);
  let error = $state("");
  let state = $state("loading");
  let preview = $state(null);

  const platformLabels = {
    youtube: "YouTube Shorts",
    x: "X",
    instagram: "Instagram Reels",
    tiktok: "TikTok",
    threads: "Threads",
  };

  onMount(async () => {
    try {
      [providers, readiness] = await Promise.all([api.providers(), api.healthStatus()]);
      state = "ready";
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
      state = "error";
    }
  });

  function toggle(platform) {
    selected = selected.includes(platform)
      ? selected.filter((item) => item !== platform)
      : [...selected, platform];
  }

  function isAvailable(platform) {
    const entry = providers?.providers?.[platform] ?? providers?.destinations?.find((item) => item.name === platform);
    return Boolean(entry);
  }

  function canPost(platform) {
    const entry = readiness?.canonical?.providers?.[platform]?.role_status?.video_destination;
    return entry?.state === "ready" || readiness?.auth?.[platform]?.session_valid === true;
  }

  function handleSubmit(event) {
    event.preventDefault();
    preview = {
      status: "plan_only",
      file: filePath,
      platforms: selected,
      caption,
      message: "This foundation prepares the exact post request. Upload execution is intentionally gated until the shared post job API is connected.",
    };
  }
</script>

<header class="xpst-page-header">
  <div>
    <h1>Create</h1>
    <p>Choose one local video, confirm destinations, and review readiness before publishing.</p>
  </div>
</header>

{#if state === "loading"}
  <p class="xpst-page-header">Loading creator workspace…</p>
{:else if state === "error"}
  <ErrorState message={error} />
{:else}
  <form class="xpst-create-layout" onsubmit={handleSubmit}>
    <Card title="Content" description="The current foundation never uploads without an explicit execution contract.">
      <FormField label="Video path" hint="Use a local .mp4 or .mov path. Directory and unsupported-file checks happen in shared preflight.">
        <input bind:value={filePath} class="xpst-input" placeholder="/path/to/video.mp4" required />
      </FormField>
      <FormField label="Caption" hint={`${caption.length} characters`}>
        <textarea bind:value={caption} class="xpst-input xpst-input--textarea" rows="5" placeholder="Write the caption once; platform-specific overrides come in the execution workflow."></textarea>
      </FormField>
      <Button type="submit" disabled={!filePath || selected.length === 0}>Review post plan</Button>
    </Card>

    <Card title="Destinations" description="Only choose roles the provider catalog exposes. Live readiness is shown before execution.">
      <div class="xpst-create-platforms">
        {#each platformOrder as platform}
          <label class="xpst-create-platform" class:is-selected={selected.includes(platform)}>
            <input type="checkbox" checked={selected.includes(platform)} onchange={() => toggle(platform)} disabled={!isAvailable(platform)} />
            <PlatformBadge {platform} />
            <span>
              <strong>{platformLabels[platform]}</strong>
              <small>{canPost(platform) ? "Ready to post" : "Needs review or connection"}</small>
            </span>
          </label>
        {/each}
      </div>
    </Card>
  </form>

  {#if preview}
    <Card title="Post plan" description={preview.message}>
      <dl class="xpst-plan-summary">
        <div><dt>File</dt><dd>{preview.file}</dd></div>
        <div><dt>Destinations</dt><dd>{preview.platforms.map((item) => platformLabels[item]).join(", ")}</dd></div>
        <div><dt>Caption</dt><dd>{preview.caption || "—"}</dd></div>
      </dl>
    </Card>
  {/if}
{/if}
