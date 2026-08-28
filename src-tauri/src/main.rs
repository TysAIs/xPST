// xPST Tauri 2 shell POC — throwaway prototype.
// Loads the FastAPI dashboard (dev URL) / bundled UI (release).
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    xpst_tauri_poc_lib::run()
}
