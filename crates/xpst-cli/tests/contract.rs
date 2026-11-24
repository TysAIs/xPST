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
fn explicit_json_flag_wins_over_a_tty_on_an_unimplemented_command() {
    // Automatic JSON only happens when stdout is piped; a caller that asks for
    // `--json` on a terminal must still get the machine payload. The flag is
    // read by argument id, so this also pins that the id matches Click's flag.
    let (code, out, err) = run(&["post", "--json"], true);
    assert_eq!(code, 1, "stderr: {err}");
    let payload: serde_json::Value = serde_json::from_str(out.trim())
        .unwrap_or_else(|error| panic!("--json on a TTY printed {out:?}: {error}"));
    assert_eq!(payload["error"]["code"], "NOT_IMPLEMENTED");
}

#[test]
fn root_level_json_flag_is_honoured_on_a_tty() {
    // The root group carries its own `--json` (`ctx.obj["json"]` in Click).
    // It was ignored for the same reason as the leaf flags: the root declares
    // it as `json_output`, so an id lookup by that name found nothing.
    let (code, out, err) = run(&["--json", "post"], true);
    assert_eq!(code, 1, "stderr: {err}");
    let payload: serde_json::Value = serde_json::from_str(out.trim())
        .unwrap_or_else(|error| panic!("`xpst --json post` printed {out:?}: {error}"));
    assert_eq!(payload["error"]["code"], "NOT_IMPLEMENTED");
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

/// Click converts `IntParamType`/`IntRange` options before the command body
/// runs, so a value that does not convert is a usage error: exit 2, empty
/// stdout, and Click's own message on stderr. The Rust engine used to accept
/// `--port abc` and print `http://127.0.0.1:abc/bio`.
#[test]
fn non_integer_options_are_usage_errors_with_clicks_message() {
    let cases: [(&[&str], &str, &str); 5] = [
        (
            &["bio", "--port", "abc", "--json"],
            "Usage: xpst bio [OPTIONS]",
            "Error: Invalid value for '--port' / '-p': 'abc' is not a valid integer.",
        ),
        (
            &["pair", "--ttl", "abc", "--json"],
            "Usage: xpst pair [OPTIONS]",
            "Error: Invalid value for '--ttl': 'abc' is not a valid integer.",
        ),
        (
            &["state", "backup", "--keep", "abc", "--json"],
            "Usage: xpst state backup [OPTIONS]",
            "Error: Invalid value for '--keep': 'abc' is not a valid integer.",
        ),
        (
            &["stream", "relay-config", "--rtmp-port", "abc", "--json"],
            "Usage: xpst stream relay-config [OPTIONS]",
            "Error: Invalid value for '--rtmp-port': 'abc' is not a valid integer.",
        ),
        (
            &[
                "generate", "ideas", "--topic", "x", "--count", "999", "--json",
            ],
            "Usage: xpst generate ideas [OPTIONS]",
            "Error: Invalid value for '--count' / '-n': 999 is not in the range 1<=x<=10.",
        ),
    ];
    for (args, usage, expected) in cases {
        let (code, out, err) = run(args, false);
        assert_eq!(code, 2, "`{}` must be a usage error: {err}", args.join(" "));
        assert!(
            out.is_empty(),
            "`{}` must print nothing on stdout, got: {out}",
            args.join(" ")
        );
        assert!(
            err.starts_with(usage),
            "`{}` must start with Click's usage line.\n  expected: {usage}\n  got: {err}",
            args.join(" ")
        );
        assert!(
            err.contains(expected),
            "`{}` must print Click's message.\n  expected: {expected}\n  got: {err}",
            args.join(" ")
        );
    }
}
