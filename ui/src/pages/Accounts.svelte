<script>
  import { onMount } from "svelte";
  import { api } from "../lib/api.js";

  let health = $state(null);
  let error = $state(null);

  onMount(async () => {
    try {
      health = await api.healthStatus();
    } catch (e) {
      error = e.message;
    }
  });

  const auth = $derived(health?.auth ?? {});
  const authKeys = $derived(Object.keys(auth));

  function livenessColor(entry) {
    if (entry?.session_valid) return "var(--xpst-success-text)";
    if (entry?.error === "disabled") return "var(--xpst-text-muted)";
    return "var(--xpst-danger-text)";
  }
</script>

<h1 class="mb-6 text-2xl font-bold tracking-tight">Accounts</h1>

{#if error}
  <p style="color: var(--xpst-danger-text)">Failed to load: {error}</p>
{:else if !health}
  <p style="color: var(--xpst-text-muted)">Loading…</p>
{:else}
  <div
    class="overflow-hidden rounded-2xl"
    style="background: var(--xpst-surface); box-shadow: var(--xpst-shadow);"
  >
    <table class="w-full text-left text-sm">
      <thead>
        <tr class="border-b" style="border-color: var(--xpst-border); color: var(--xpst-text-muted)">
          <th class="px-4 py-3 font-medium">Platform</th>
          <th class="px-4 py-3 font-medium">Auth mode</th>
          <th class="px-4 py-3 font-medium">Live session</th>
          <th class="px-4 py-3 font-medium">Age (days)</th>
        </tr>
      </thead>
      <tbody>
        {#each authKeys as name (name)}
          {@const entry = auth[name]}
          <tr class="border-b last:border-0" style="border-color: var(--xpst-border)">
            <td class="px-4 py-3 font-medium">{name}</td>
            <td class="px-4 py-3">{entry.auth_mode ?? "—"}</td>
            <td class="px-4 py-3" style="color: {livenessColor(entry)}">
              {entry.error === "disabled" ? "disabled" : entry.session_valid ? "valid" : "invalid"}
            </td>
            <td class="px-4 py-3">{entry.session_age_days ?? "—"}</td>
          </tr>
        {/each}
        {#if authKeys.length === 0}
          <tr><td colspan="4" class="px-4 py-6 text-center" style="color: var(--xpst-text-muted)">
            No auth data available{health.auth_error ? ` (${health.auth_error})` : ""}.
          </td></tr>
        {/if}
      </tbody>
    </table>
  </div>
{/if}
