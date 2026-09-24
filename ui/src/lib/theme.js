/*
 * Theme preference store.
 *
 * Three states: "auto" (follow the OS), "light", "dark". The choice is kept in
 * localStorage under one key so the engine (which owns no UI state) never has
 * to store a presentation preference. The DOM contract is a single attribute:
 * `data-theme` on <html>, set to "light" or "dark" for an explicit choice and
 * REMOVED for "auto" — tokens.css already implements exactly that contract
 * (`:root[data-theme="dark"]`, `:root:not([data-theme="light"])` under the OS
 * media query).
 *
 * Everything here degrades to "auto" when localStorage is unavailable (the
 * engine also serves this UI to plain browsers with storage disabled).
 */

const STORAGE_KEY = "xpst.theme";

/** @returns {"auto" | "light" | "dark"} */
export function storedTheme() {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY);
    return value === "light" || value === "dark" ? value : "auto";
  } catch {
    return "auto";
  }
}

/** Apply a theme choice to the document. "auto" removes the attribute. */
export function applyTheme(theme) {
  const choice = theme === "light" || theme === "dark" ? theme : "auto";
  try {
    if (choice === "auto") {
      window.localStorage.removeItem(STORAGE_KEY);
    } else {
      window.localStorage.setItem(STORAGE_KEY, choice);
    }
  } catch {
    // Storage unavailable (private mode): the choice still applies live.
  }
  const root = document.documentElement;
  if (choice === "auto") {
    root.removeAttribute("data-theme");
  } else {
    root.setAttribute("data-theme", choice);
  }
  return choice;
}

/** The three options the Settings control offers, with honest labels. */
export const THEME_OPTIONS = [
  { value: "auto", label: "Auto — match my Mac" },
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
];
