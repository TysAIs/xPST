<script>
  import { onMount } from "svelte";
  import { api, isLandingHash, NAV_ITEMS, currentRoute } from "./lib/api.js";
  import { shouldStartOnboarding } from "./lib/firstRun.js";
  import Shell from "./lib/components/Shell.svelte";
  import Dashboard from "./pages/Dashboard.svelte";
  import Onboarding from "./pages/Onboarding.svelte";
  import Connect from "./pages/Connect.svelte";
  import Compose from "./pages/Compose.svelte";
  import Result from "./pages/Result.svelte";
  import Create from "./pages/Create.svelte";
  import Analytics from "./pages/Analytics.svelte";
  import Videos from "./pages/Videos.svelte";
  import Accounts from "./pages/Accounts.svelte";
  import Schedule from "./pages/Schedule.svelte";
  import Activity from "./pages/Activity.svelte";
  import Library from "./pages/Library.svelte";
  import About from "./pages/About.svelte";
  import Settings from "./pages/Settings.svelte";

  let route = $state(currentRoute());
  const pages = {
    dashboard: Dashboard,
    onboarding: Onboarding,
    connect: Connect,
    compose: Compose,
    result: Result,
    create: Create,
    analytics: Analytics,
    videos: Videos,
    accounts: Accounts,
    schedule: Schedule,
    activity: Activity,
    library: Library,
    about: About,
    settings: Settings,
  };
  const Page = $derived(pages[route] ?? Dashboard);

  // First-run only: a brand-new install lands here with first_run_complete
  // false, so the wizard opens once. An explicit navigation to any section is
  // never hijacked, and a failing onboarding call never redirects (the user
  // must not be bounced by an error).
  let firstRunChecked = false;

  async function maybeStartOnboarding() {
    if (firstRunChecked) return;
    firstRunChecked = true;
    if (!isLandingHash()) return;
    try {
      const payload = await api.onboarding();
      if (shouldStartOnboarding(payload, location.hash)) {
        location.hash = "#/onboarding";
        updateRoute();
      }
    } catch {
      // Leave the user on the dashboard; the Setup nav item stays available.
    }
  }

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
    maybeStartOnboarding();

    return () => {
      window.removeEventListener("hashchange", updateRoute);
      window.removeEventListener("popstate", updateRoute);
    };
  });
</script>

<Shell items={NAV_ITEMS} {route} onNavigate={onNav}>
  <Page />
</Shell>