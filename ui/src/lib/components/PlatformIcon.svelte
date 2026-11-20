<script>
  import {
    AtSign,
    Camera,
    Gamepad2,
    Globe2,
    MessageCircle,
    Music2,
    Play,
    Radio,
  } from "@lucide/svelte";

  const PLATFORM_ICONS = {
    youtube: Play,
    instagram: Camera,
    x: AtSign,
    tiktok: Music2,
    threads: MessageCircle,
    // The live-chat platforms. Lucide ships no brand marks, so each row gets a
    // generic icon that says what the platform *is* in this app: a broadcast
    // (Twitch) and a game stream (Kick).
    twitch: Radio,
    kick: Gamepad2,
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
