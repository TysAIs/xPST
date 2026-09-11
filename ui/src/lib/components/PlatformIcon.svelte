<script>
  import {
    AtSign,
    Camera,
    Globe2,
    MessageCircle,
    Music2,
    Play,
  } from "@lucide/svelte";

  const PLATFORM_ICONS = {
    youtube: Play,
    instagram: Camera,
    x: AtSign,
    tiktok: Music2,
    threads: MessageCircle,
    other: Globe2,
  };

  let { platform = "other", size = 18, strokeWidth = 2, ...rest } = $props();
  const normalized = $derived(String(platform || "other").toLowerCase());
  const Icon = $derived(PLATFORM_ICONS[normalized] ?? Globe2);
  const platformClass = $derived(normalized in PLATFORM_ICONS ? normalized : "other");
</script>

<span class="xpst-platform-badge__icon" data-platform={platformClass}>
  <Icon size={size} strokeWidth={strokeWidth} aria-hidden="true" {...rest} />
</span>
