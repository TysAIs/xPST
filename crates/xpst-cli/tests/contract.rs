//! The P0 contract for commands whose behavior has not moved yet.
//!
//! Posting is paused and the Python engine is still the shipped surface, so the
//! one thing this binary must never do is *pretend*. Every unimplemented
//! command has to say so in terms both a human and a script can act on, and it
//! must not exit 0.

use std::ffi::OsString;

use xpst_cli::run_with_io;

fn run(args: &[&str], stdout_is_tty: bool) -> (i32, String, String) {
    let mut argv: Vec<OsString> = vec![OsString::from("xpst")];
    argv.extend(args.iter().map(OsString::from));
    let mut out: Vec<u8> = Vec::new();
    let mut err: Vec<u8> = Vec::new();
    let code = run_with_io(argv, &mut out, &mut err, stdout_is_tty);
    (
        code,
        String::from_utf8_lossy(&out).into_owned(),
        String::from_utf8_lossy(&err).into_owned(),
    )
}

#[test]
fn unimplemented_command_says_so_on_a_tty_and_exits_nonzero() {
    let (code, _out, err) = run(&["run"], true);
    assert_eq!(code, 1, "an unimplemented command must not exit 0");
    assert!(
        err.contains("not implemented in the Rust engine yet"),
        "expected the documented message, got: {err}"
    );
    assert!(
        err.contains("The Python engine is still the shipped surface"),
        "the message must point at the working engine: {err}"
    );
}

#[test]
fn unimplemented_command_emits_a_machine_readable_error() {
    let (code, out, _err) = run(&["post", "--json"], false);
    assert_eq!(code, 1);
    let payload: serde_json::Value = serde_json::from_str(out.trim()).expect("valid JSON");
    assert_eq!(payload["ok"], false);
    assert_eq!(payload["error"]["code"], "NOT_IMPLEMENTED");
    assert_eq!(payload["error"]["command"], "post");
    assert_eq!(payload["error"]["engine"], "rust");
    assert_eq!(
        payload["error"]["message"],
        "not implemented in the Rust engine yet"
    );
}

#[test]
fn nested_unimplemented_commands_report_their_full_path() {
    let (code, out, err) = run(
        &[
            "schedule",
            "add",
            "--caption",
            "hi",
            "--at",
            "2026-01-01T00:00:00",
            "--json",
        ],
        false,
    );
    assert_eq!(code, 1, "stderr: {err}");
    let payload: serde_json::Value = serde_json::from_str(out.trim()).expect("valid JSON");
    assert_eq!(payload["error"]["command"], "schedule add");
}

#[test]
fn required_options_are_enforced_before_dispatch_like_click() {
    // `schedule add` requires --caption and --at in the Python CLI too, and
    // Click exits 2 for a missing required option. A usage error must not be
    // mistaken for "not implemented".
    let (code, out, err) = run(&["schedule", "add", "--json"], false);
    assert_eq!(code, 2, "missing required option is a usage error");
    assert!(err.contains("--caption"), "{err}");
    assert!(!out.contains("NOT_IMPLEMENTED"), "{out}");
}

#[test]
fn json_mode_is_automatic_when_stdout_is_not_a_tty() {
    // `cli.py` sets ctx.obj["json"] when stdout is piped; scripts rely on it.
    let (_, piped, _) = run(&["run"], false);
    assert!(
        piped.trim_start().starts_with('{'),
        "piped output must be JSON: {piped}"
    );
}

#[test]
fn a_group_invoked_bare_prints_usage_and_exits_2() {
    let (code, _out, err) = run(&["config"], true);
    assert_eq!(code, 2, "Click exits 2 for a group with no subcommand");
    assert!(!err.is_empty(), "usage should be reported");
}

#[test]
fn help_exits_0_and_covers_the_top_level_surface() {
    let (code, out, _err) = run(&["--help"], false);
    assert_eq!(code, 0);
    for command in ["post", "schedule", "doctor", "config", "mcp", "state"] {
        assert!(
            out.lines()
                .any(|line| line.trim_start().starts_with(command)),
            "`xpst --help` is missing `{command}`"
        );
    }
}

#[test]
fn unknown_flag_is_a_usage_error_not_a_crash() {
    let (code, _out, err) = run(&["config", "show", "--not-a-flag"], false);
    assert_eq!(code, 2, "usage errors are exit code 2");
    assert!(
        err.contains("--not-a-flag") || err.contains("unexpected"),
        "{err}"
    );
}

#[test]
fn implemented_commands_are_not_reported_as_unimplemented() {
    for args in [vec!["doctor"], vec!["version"], vec!["config", "validate"]] {
        let (code, out, err) = run(&args, true);
        assert_ne!(
            code,
            1,
            "`{}` regressed to unimplemented: {err}",
            args.join(" ")
        );
        assert!(
            !out.contains("NOT_IMPLEMENTED") && !err.contains("NOT_IMPLEMENTED"),
            "`{}` reported NOT_IMPLEMENTED",
            args.join(" ")
        );
    }
}
