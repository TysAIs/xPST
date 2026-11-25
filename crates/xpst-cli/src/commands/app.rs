//! `xpst app` — launch the installed desktop shell.
//!
//! Parity target: `src/xpst/cli.py::app`. The shell is a native binary or
//! `.app` bundle (`com.tysais.xpst`), not a Python module, so it is located on
//! disk in the same preference order Python uses and then spawned. When nothing
//! is installed the command prints the same three lines of guidance and exits 1.
//!
//! `com.tysais.xpst` is the product's shipped reverse-DNS bundle identifier
//! (`src-tauri/tauri.conf.json`), not personal data.

use std::io::Write;
use std::path::PathBuf;

use clap::ArgMatches;

use crate::output::{wrap_at, write_text};
use crate::{exit, GlobalOptions};

/// Rich's default console width when stdout is not a TTY.
const WIDTH: usize = 80;

/// The installed shell locations, in preference order (`_desktop_shell_candidates`).
pub fn candidates() -> Vec<PathBuf> {
    let home = std::env::var_os("HOME").map(PathBuf::from);
    if cfg!(target_os = "macos") {
        let mut paths = vec![PathBuf::from("/Applications/xPST.app")];
        if let Some(home) = &home {
            paths.push(home.join("Applications").join("xPST.app"));
        }
        return paths;
    }
    if cfg!(windows) {
        let mut paths = Vec::new();
        if let Some(local) = std::env::var_os("LOCALAPPDATA") {
            paths.push(PathBuf::from(local).join("xPST").join("xPST.exe"));
        }
        for variable in ["ProgramFiles", "ProgramFiles(x86)"] {
            if let Some(base) = std::env::var_os(variable) {
                paths.push(PathBuf::from(base).join("xPST").join("xPST.exe"));
            }
        }
        return paths;
    }
    // Linux: the .deb installs `xPST` (capitalised — the lowercase `xpst` on
    // PATH is this CLI), the AppImage is usually kept in ~/Applications.
    let mut paths = vec![
        PathBuf::from("/usr/bin/xPST"),
        PathBuf::from("/usr/local/bin/xPST"),
    ];
    if let Some(home) = &home {
        paths.push(home.join("Applications").join("xPST.AppImage"));
    }
    paths
}

/// Run `app`.
pub fn run(
    _global: &GlobalOptions,
    _leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let Some(target) = candidates().into_iter().find(|path| path.exists()) else {
        let _ = write_text(out, "xPST desktop app not found.");
        let _ = write_text(
            out,
            &wrap_at(
                "Install a published bundle from https://github.com/TysAIs/xPST/releases",
                WIDTH,
            ),
        );
        let _ = write_text(
            out,
            &wrap_at(
                "or build one from a checkout: scripts/build-engine.sh && (cd src-tauri && cargo tauri build)",
                WIDTH,
            ),
        );
        return exit::GENERAL;
    };

    let _ = write_text(
        out,
        &format!("Launching xPST desktop app… {}", target.display()),
    );
    let spawned = if cfg!(target_os = "macos") {
        std::process::Command::new("open").arg(&target).spawn()
    } else {
        std::process::Command::new(&target).spawn()
    };
    if let Err(error) = spawned {
        let _ = writeln!(err, "xpst app: cannot launch {}: {error}", target.display());
        return exit::GENERAL;
    }
    exit::SUCCESS
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_not_found_guidance_wraps_at_eighty_columns() {
        let wrapped = wrap_at(
            "or build one from a checkout: scripts/build-engine.sh && (cd src-tauri && cargo tauri build)",
            WIDTH,
        );
        assert_eq!(
            wrapped,
            "or build one from a checkout: scripts/build-engine.sh && (cd src-tauri && cargo \ntauri build)"
        );
    }

    #[test]
    fn linux_candidates_are_the_documented_three() {
        if cfg!(target_os = "linux") {
            let paths = candidates();
            assert_eq!(paths[0], PathBuf::from("/usr/bin/xPST"));
            assert_eq!(paths[1], PathBuf::from("/usr/local/bin/xPST"));
            assert!(paths[2].ends_with("Applications/xPST.AppImage"));
        }
    }
}
