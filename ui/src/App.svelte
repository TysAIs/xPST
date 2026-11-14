<script>
  import { onMount } from "svelte";
  import { api, isLandingHash, NAV_ITEMS, currentRoute } from "./lib/api.js";
  import { shouldStartOnboarding } from "./lib/firstRun.js";
  import { storedTheme, applyTheme } from "./lib/theme.js";
  import { checkForUpdates, describeUpdateResult, installUpdate, updatesAvailable } from "./lib/updates.js";
  import Shell from "./lib/components/Shell.svelte";
  import Dashboard from "./pages/Dashboard.svelte";
  import Onboarding from "./pages/Onboarding.svelte";
  import Connect from "./pages/Connect.svelte";
  import Compose from "./pages/Compose.svelte";
  import Phone from "./pages/Phone.svelte";
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
    phone: Phone,
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

  // Updates are checked once per launch, by default. This is the whole point
  // of the check: a user who installed xPST should not have to know that a
  // newer release exists. Installing is still a user action.
  let updateState = $state({ status: "idle" });
  let installing = $state(false);

  async function refreshUpdateState() {
    updateState = { status: "checking" };
    updateState = await checkForUpdates();
  }

  async function installAvailableUpdate() {
    installing = true;
    const result = await installUpdate();
    installing = false;
    if (!result.ok) {
      updateState = { status: "error", error: result.error };
    }
  }

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
    // Restore the saved appearance before first paint of any page content.
    applyTheme(storedTheme());
    window.addEventListener("hashchange", updateRoute);
    window.addEventListener("popstate", updateRoute);
    updateRoute();
    maybeStartOnboarding();
    // Default-on update check (shell only; resolves to "unavailable" in a
    // browser). Deliberately after routing so a slow network cannot delay the
    // first screen.
    if (updatesAvailable()) refreshUpdateState();

    return () => {
      window.removeEventListener("hashchange", updateRoute);
      window.removeEventListener("popstate", updateRoute);
    };
  });
</script>

<Shell items={NAV_ITEMS} {route} onNavigate={onNav}>
  {#if updateState.status === "available"}
    <aside class="xpst-update-banner" role="status" aria-live="polite">
      <span>{describeUpdateResult(updateState)}</span>
      <span class="xpst-update-banner__actions">
        <button type="button" class="xpst-button" data-size="sm" onclick={installAvailableUpdate} disabled={installing}>
          {installing ? "Installing…" : "Install and restart"}
        </button>
        <a href="#/about">Details</a>
      </span>
    </aside>
  {/if}
  <Page />
</Shell>