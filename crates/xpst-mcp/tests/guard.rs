//! Consent tiering: mutating tools are refused unless explicitly allowed, and
//! the refusal text is the Python text.

use serde_json::json;
use xpst_mcp::guard::ConsentPolicy;
use xpst_mcp::registry::registry;

fn entry(name: &str) -> &'static xpst_mcp::ToolEntry {
    registry()
        .get(name)
        .unwrap_or_else(|| panic!("{name} missing"))
}

#[test]
fn read_only_tools_need_no_consent() {
    let policy = ConsentPolicy::default();
    for tool in [
        "xpst_status",
        "xpst_config_show",
        "xpst_schedule_list",
        "xpst_rate_limit_status",
        "xpst_providers",
    ] {
        assert!(
            policy.permits(entry(tool)).is_ok(),
            "{tool} must be free to call"
        );
        assert!(policy.confirms(entry(tool), &json!({})).is_ok());
    }
}

#[test]
fn mutating_tools_are_refused_by_default() {
    let policy = ConsentPolicy::default();
    let err = policy
        .permits(entry("xpst_post"))
        .expect_err("default denies");
    assert_eq!(err.code(), "consent_denied");
    let message = err.to_string();
    assert!(
        message.contains("xpst_post posts to or mutates REAL accounts."),
        "{message}"
    );
    assert!(message.contains("XPST_MCP_ALLOW_MUTATIONS=1"), "{message}");
    assert!(message.contains("XPST_MCP_REQUIRE_CONFIRM=1"), "{message}");
}

#[test]
fn read_only_mode_blocks_even_an_allowed_mutation() {
    let policy = ConsentPolicy {
        read_only: true,
        allow_mutations: true,
        require_confirm: false,
    };
    let err = policy
        .permits(entry("xpst_run"))
        .expect_err("read-only denies");
    assert_eq!(
        err.to_string(),
        "Blocked: xpst_run is disabled (XPST_MCP_READONLY is set). \
         Unset it to allow posting/mutating tools."
    );
}

#[test]
fn allowed_mutations_pass_without_confirmation() {
    let policy = ConsentPolicy {
        read_only: false,
        allow_mutations: true,
        require_confirm: false,
    };
    assert!(policy.permits(entry("xpst_delete")).is_ok());
    assert!(policy.confirms(entry("xpst_delete"), &json!({})).is_ok());
}

#[test]
fn confirmation_mode_requires_confirm_true_per_call() {
    let policy = ConsentPolicy {
        read_only: false,
        allow_mutations: false,
        require_confirm: true,
    };
    assert!(policy.permits(entry("xpst_disconnect")).is_ok());

    let err = policy
        .confirms(entry("xpst_disconnect"), &json!({}))
        .expect_err("missing confirm");
    assert!(err
        .to_string()
        .contains("Re-call with \"confirm\": true to proceed."));

    assert!(policy
        .confirms(entry("xpst_disconnect"), &json!({"confirm": true}))
        .is_ok());
    // A non-boolean truthy value is not confirmation, matching Python's
    // `arguments.get("confirm")` on a real bool field.
    assert!(policy
        .confirms(entry("xpst_disconnect"), &json!({"confirm": "true"}))
        .is_err());
}

#[test]
fn pausing_is_deliberately_not_gated_but_resuming_is() {
    assert!(entry("xpst_schedule_pause").is_read_only());
    assert!(!entry("xpst_schedule_resume").is_read_only());
    assert!(entry("xpst_setup_start").is_read_only());
}
