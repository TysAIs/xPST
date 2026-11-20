// Update checks for the desktop shell.
//
// The updater lives in the Tauri shell, not in the engine: only the shell can
// replace its own binary, and only the shell carries the release public key.
// This module is the UI's thin view over two shell commands:
//
//   updater_check   -> null when current, or {version, current_version, notes, date}
//   updater_install -> downloads, verifies the signature, installs, restarts
//
// Nothing here installs anything on its own. The app calls `checkForUpdates()`
// once at start (so an available update is visible without hunting for a menu),
// and installation happens only when the user presses the button.
//
// In a plain browser (`npm run dev`, or the engine served outside the shell)
// there is no IPC transport; this module reports "unavailable" instead of
// pretending an update check happened.

const CHECK_COMMAND = "updater_check";
const INSTALL_COMMAND = "updater_install";

/** The Tauri IPC transport, or null in a browser. */
function transport() {
  const api = globalThis.__TAURI_INTERNALS__;
  return api && typeof api.invoke === "function" ? api : null;
}

/** True when this page is running inside the desktop shell. */
export function updatesAvailable() {
  return Boolean(transport());
}

/**
 * Human sentence for one check result. Never claims an update exists unless
 * the shell said so.
 */
export function describeUpdateResult(result) {
  switch (result?.status) {
    case "available":
      return `xPST ${result.update?.version ?? "a newer version"} is available.`;
    case "current":
      return "xPST is up to date.";
    case "unavailable":
      return "Update checks need the xPST desktop app; this page is open outside it.";
    case "error":
      return `Could not check for updates: ${result.error ?? "unknown error"}`;
    case "checking":
      return "Checking for updates…";
    default:
      return "Not checked yet.";
  }
}

/** Release notes text for an available update, or "" when the release has none. */
export function updateNotes(result) {
  const notes = result?.update?.notes;
  return typeof notes === "string" ? notes.trim() : "";
}

/**
 * Ask the shell whether a newer release exists.
 *
 * Resolves to one of:
 *   { status: "available", update }
 *   { status: "current" }
 *   { status: "unavailable", error }
 *   { status: "error", error }
 */
export async function checkForUpdates() {
  const api = transport();
  if (!api) {
    return { status: "unavailable", error: "the desktop shell is not available" };
  }
  try {
    const update = await api.invoke(CHECK_COMMAND);
    if (update && typeof update === "object" && update.version) {
      return { status: "available", update };
    }
    return { status: "current" };
  } catch (cause) {
    return { status: "error", error: cause?.message ?? String(cause) };
  }
}

/**
 * Install the update the user just chose. Resolves only on failure: a
 * successful install restarts the app, so this promise normally never settles.
 *
 * Resolves to `{ ok: false, error }` when the install did not happen.
 */
export async function installUpdate() {
  const api = transport();
  if (!api) {
    return { ok: false, error: "the desktop shell is not available" };
  }
  try {
    const version = await api.invoke(INSTALL_COMMAND);
    // Reaching this line means no restart happened: say so instead of leaving
    // the user staring at a spinner.
    return { ok: false, error: `xPST ${version ?? ""} was installed but did not restart; quit and reopen it.` };
  } catch (cause) {
    return { ok: false, error: cause?.message ?? String(cause) };
  }
}
