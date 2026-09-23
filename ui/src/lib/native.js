// Shell bridge: the desktop window can hand the composer *real filesystem
// paths* — from the OS file picker and from OS drag-and-drop.
//
// A plain browser cannot do either honestly: a dropped File has no path, and
// the only way to preview it would be to read its bytes into the page, which
// is exactly the full-file copy this feature exists to avoid. So this module
// reports "unavailable" outside the shell and the page keeps working with the
// folder list — it never pretends a file was chosen.
//
// Transport notes:
// * Picker: one IPC call to the dialog plugin (`plugin:dialog|open`). The
//   shell grants that command to the loopback engine origin in
//   src-tauri/capabilities/default.json; if the grant is missing the call
//   rejects and we surface the shell's own message instead of a fake path.
// * Drag-and-drop: the shell owns the native drop (so it has the paths) and
//   pushes them into the page by evaluating `window.__xpstMediaDrag(...)`.
//   `installShellDropTarget` registers that global.

import { MEDIA_EXTENSIONS } from "./media.js";

const DIALOG_OPEN = "plugin:dialog|open";
const DRAG_HOOK = "__xpstMediaDrag";

/** The Tauri IPC transport, or null in a browser. */
function transport() {
  const api = globalThis.__TAURI_INTERNALS__;
  return api && typeof api.invoke === "function" ? api : null;
}

/** True when this page is running inside the desktop shell. */
export function shellAvailable() {
  return Boolean(transport());
}

/** Extension filters in the shape the dialog plugin expects (no leading dot). */
function dialogExtensions(extensions) {
  return extensions.map((extension) => String(extension).replace(/^\./, ""));
}

/**
 * Open the OS file picker for one media file.
 *
 * Resolves to `{ ok: true, path }`, or `{ ok: false, reason }` where reason is
 * "unavailable" (plain browser), "cancelled" (user closed the dialog), or
 * "denied" (the shell refused the IPC call — carried with its error text).
 */
export async function pickMediaFile({ extensions = MEDIA_EXTENSIONS, title = "Choose media for this post" } = {}) {
  const api = transport();
  if (!api) {
    return { ok: false, reason: "unavailable", error: "The file picker is only available in the xPST app window." };
  }
  let selected;
  try {
    selected = await api.invoke(DIALOG_OPEN, {
      options: {
        multiple: false,
        directory: false,
        title,
        filters: [{ name: "Media", extensions: dialogExtensions(extensions) }],
      },
    });
  } catch (cause) {
    return { ok: false, reason: "denied", error: cause?.message ?? String(cause) };
  }
  const path = Array.isArray(selected) ? selected[0] : selected;
  if (typeof path !== "string" || !path) return { ok: false, reason: "cancelled" };
  return { ok: true, path };
}

/**
 * Register the handler the shell calls on native drag-and-drop.
 *
 * `handler(phase, paths)` receives phase "enter" | "drop" | "leave"; only
 * "drop" carries paths. Returns an unsubscribe function.
 */
export function installShellDropTarget(handler) {
  globalThis[DRAG_HOOK] = (phase, paths) => {
    try {
      handler(String(phase ?? "drop"), Array.isArray(paths) ? paths : []);
    } catch (cause) {
      // A UI error must never take the window (or the drop) down with it.
      console.error("[xpst] media drop handler failed", cause);
    }
  };
  return () => {
    if (globalThis[DRAG_HOOK]) delete globalThis[DRAG_HOOK];
  };
}
