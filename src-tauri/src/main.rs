// xPST desktop shell — Tauri 2 wrapper around the Python engine sidecar.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    xpst_shell_lib::run()
}
