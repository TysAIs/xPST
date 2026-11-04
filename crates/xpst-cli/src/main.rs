//! The `xpst` binary entry point.
//!
//! All logic lives in the library so the command tree, the exit codes and the
//! read paths can be tested without spawning a process.

fn main() {
    let args: Vec<std::ffi::OsString> = std::env::args_os().collect();
    std::process::exit(xpst_cli::run(args));
}
