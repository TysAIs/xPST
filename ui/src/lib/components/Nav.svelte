<script>
  import { LayoutDashboard, ChartNoAxesCombined, Video, Users, Settings, CalendarClock, TriangleAlert, Library as LibraryIcon, Info } from "@lucide/svelte";
  import { NAV_ITEMS } from "../api.js";

  const ICONS = {
    "layout-dashboard": LayoutDashboard,
    "chart-no-axes-combined": ChartNoAxesCombined,
    video: Video,
    users: Users,
    "calendar-clock": CalendarClock,
    "triangle-alert": TriangleAlert,
    library: LibraryIcon,
    info: Info,
    settings: Settings,
  };

  let { items = NAV_ITEMS, route = "dashboard", onNavigate = undefined, mobile = false } = $props();
</script>

<nav class:xpst-nav--mobile={mobile} class="xpst-nav" aria-label={mobile ? "Primary mobile" : "Primary"}>
  <ul class="xpst-nav__list">
    {#each items as item (item.id)}
      {@const Icon = ICONS[item.icon] ?? LayoutDashboard}
      <li>
        <a
          class="xpst-nav__link"
          href={item.href}
          aria-current={route === item.id ? "page" : undefined}
          onclick={() => onNavigate?.(item.id)}
        >
          <span class="xpst-nav__icon" aria-hidden="true"><Icon size={19} strokeWidth={2} /></span>
          <span class:sr-only={mobile}>{item.label}</span>
        </a>
      </li>
    {/each}
  </ul>
</nav>
