<script>
  import { NAV_ITEMS, currentRoute } from "./lib/api.js";
  import Dashboard from "./pages/Dashboard.svelte";
  import Analytics from "./pages/Analytics.svelte";
  import Videos from "./pages/Videos.svelte";
  import Accounts from "./pages/Accounts.svelte";
  import Settings from "./pages/Settings.svelte";

  let route = $state(currentRoute());
  const pages = {
    dashboard: Dashboard,
    analytics: Analytics,
    videos: Videos,
    accounts: Accounts,
    settings: Settings,
  };
  const Page = $derived(pages[route] ?? Dashboard);

  function onNav() {
    route = currentRoute();
  }
</script>

<div class="flex min-h-screen">
  <!-- Sidebar -->
  <aside
    class="flex w-60 shrink-0 flex-col gap-1 p-4"
    style="background: var(--xpst-bg-elevated); border-right: 1px solid var(--xpst-border);"
  >
    <div class="mb-6 flex items-center gap-3 px-2 pt-2">
      <div
        class="flex h-10 w-10 items-center justify-center rounded-xl text-lg font-bold"
        style="background: var(--xpst-accent); color: #ffffff;"
      >X</div>
      <span class="text-lg font-semibold tracking-tight">xPST</span>
    </div>
    <nav class="flex flex-col gap-1" aria-label="Main">
      {#each NAV_ITEMS as item (item.id)}
        <a
          href={item.href}
          onclick={onNav}
          aria-current={route === item.id ? "page" : undefined}
          class="flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition-colors"
          style={
            route === item.id
              ? "background: var(--xpst-accent); color: #ffffff;"
              : "color: var(--xpst-text-secondary);"
          }
        >
          <span aria-hidden="true">{item.icon}</span>
          {item.label}
        </a>
      {/each}
    </nav>
    <footer
      class="mt-auto px-3 text-xs"
      style="color: var(--xpst-text-muted);"
    >
      Cross-posting control plane
    </footer>
  </aside>

  <!-- Content -->
  <main class="min-w-0 flex-1 p-8">
    <Page />
  </main>
</div>
