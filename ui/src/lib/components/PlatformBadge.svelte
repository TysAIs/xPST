<script>
  import PlatformIcon from "./PlatformIcon.svelte";
  import { platformLabel } from "../labels.js";

  let { platform = "other", label = undefined, size = 18 } = $props();
  const displayLabel = $derived(label ?? platformLabel(platform));
</script>

<span class="xpst-platform-badge" aria-label={displayLabel}>
  <PlatformIcon {platform} {size} />
  <span>{displayLabel}</span>
</span>
