<script>
  import {
    Clapperboard,
    ChartNoAxesCombined,
    Info,
    LayoutDashboard,
    Library as LibraryIcon,
    ListChecks,
    Plug,
    Settings,
    Sparkles,
    TriangleAlert,
    Users,
    Video,
    CalendarClock,
  } from "@lucide/svelte";
  import { NAV_ITEMS, NAV_GROUPS } from "../api.js";

  const ICONS = {
    "layout-dashboard": LayoutDashboard,
    sparkles: Sparkles,
    plug: Plug,
    clapperboard: Clapperboard,
    "list-checks": ListChecks,
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

  // Desktop sidebar renders grouped with headings; mobile (icon rail) keeps the
  // flat order so it stays one dense strip.
  const groups = $derived(
    NAV_GROUPS.map((group) => ({
      ...group,
      items: items.filter((item) => (item.group ?? "system") === group.id),
    })).filter((group) => group.items.length > 0)
  );
</script>

<nav class:xpst-nav--mobile={mobile} class="xpst-nav" aria-label={mobile ? "Primary mobile" : "Primary"}>
  {#if mobile}
    <ul class="xpst-nav__list">
      {#each items as item (item.id)}
        {@const Icon = ICONS[item.icon] ?? LayoutDashboard}
        <li>
          <a
            class="xpst-nav__link"
            href={item.href}
            aria-current={route === item.id ? "page" : undefined}
            aria-label={item.label}
            onclick={() => onNavigate?.(item.id)}
          >
            <span class="xpst-nav__icon" aria-hidden="true"><Icon size={19} strokeWidth={2} /></span>
            <span class:sr-only={mobile}>{item.label}</span>
          </a>
        </li>
      {/each}
    </ul>
  {:else}
    {#each groups as group (group.id)}
      <div class="xpst-nav__group">
        <p class="xpst-nav__group-label" id={`nav-group-${group.id}`}>{group.label}</p>
        <ul class="xpst-nav__list" aria-labelledby={`nav-group-${group.id}`}>
          {#each group.items as item (item.id)}
            {@const Icon = ICONS[item.icon] ?? LayoutDashboard}
            <li>
              <a
                class="xpst-nav__link"
                href={item.href}
                aria-current={route === item.id ? "page" : undefined}
                onclick={() => onNavigate?.(item.id)}
              >
                <span class="xpst-nav__icon" aria-hidden="true"><Icon size={19} strokeWidth={2} /></span>
                <span>{item.label}</span>
              </a>
            </li>
          {/each}
        </ul>
      </div>
    {/each}
  {/if}
</nav>
