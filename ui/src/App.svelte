<script>
  import { onMount } from "svelte";
  import { NAV_ITEMS, currentRoute } from "./lib/api.js";
  import Shell from "./lib/components/Shell.svelte";
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

  function updateRoute() {
    route = currentRoute();
  }

  function onNav() {
    // Keep active navigation responsive even when a browser reuses the hash.
    updateRoute();
  }

  onMount(() => {
    window.addEventListener("hashchange", updateRoute);
    window.addEventListener("popstate", updateRoute);
    updateRoute();

    return () => {
      window.removeEventListener("hashchange", updateRoute);
      window.removeEventListener("popstate", updateRoute);
    };
  });
</script>

<Shell items={NAV_ITEMS} {route} onNavigate={onNav}>
  <Page />
</Shell>
