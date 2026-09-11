<script>
  import PlatformIcon from "./PlatformIcon.svelte";

  let { platform = "other", label = undefined, size = 18 } = $props();
  const displayLabel = $derived(label ?? String(platform || "Other").replace(/^./, (letter) => letter.toUpperCase()));
</script>

<span class="xpst-platform-badge" aria-label={displayLabel}>
  <PlatformIcon {platform} {size} />
  <span>{displayLabel}</span>
</span>
