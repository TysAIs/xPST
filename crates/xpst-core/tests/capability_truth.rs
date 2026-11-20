//! The capability table must not claim more than this workspace can do.
//!
//! The audit finding behind this file: `content.rs` listed tiktok, threads,
//! facebook and rumble as *implemented* while their provider crates were stubs
//! or absent, and `plan()` gates acceptance on that set — so a plan accepted
//! destinations nothing could publish to.
//!
//! The rule enforced here is structural, not editorial:
//!
//! * every destination that lists an implemented content type must name a
//!   provider crate in [`DESTINATION_PROVIDER_CRATES`];
//! * that crate must exist under `crates/`, be a member of the workspace, and
//!   contain a public publishing entry point (`pub fn publish…` /
//!   `pub async fn publish…` / `pub fn upload…`) — the shape a stub cannot
//!   fake, because the four P0 stub crates contain no code at all;
//! * a destination with no provider crate must list **no** implemented content
//!   type, and the planner must refuse it with
//!   [`UNIMPLEMENTED_DESTINATION_CODE`] rather than accept it.
//!
//! Read the workspace from disk: this test's job is to fail when the table and
//! the tree disagree, so it must not depend on anything the table itself says.

use std::path::{Path, PathBuf};

use xpst_core::content::{
    provider_crate, DestinationContentProfile, DESTINATION_CONTENT_PROFILES,
    DESTINATION_PROVIDER_CRATES,
};
use xpst_core::ingest::DestinationCapability;
use xpst_core::plan::{plan, RouteRequest, UNIMPLEMENTED_DESTINATION_CODE};

fn workspace_root() -> PathBuf {
    // `CARGO_MANIFEST_DIR` is `<root>/crates/xpst-core`.
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(Path::parent)
        .expect("the crate lives two levels below the workspace root")
        .to_path_buf()
}

fn is_workspace_member(crate_name: &str) -> bool {
    let manifest = std::fs::read_to_string(workspace_root().join("Cargo.toml"))
        .expect("the workspace manifest is readable");
    manifest.contains(&format!("\"crates/{crate_name}\""))
}

/// Whether the crate carries a publishing entry point rather than a doc-comment
/// placeholder.
///
/// A stub crate is a `lib.rs` containing only the crate docs. Taking comments
/// out is what makes this a check rather than a coincidence: "publish" appears
/// in the P0 stubs' prose too.
fn has_publish_path(crate_name: &str) -> bool {
    let src = workspace_root().join("crates").join(crate_name).join("src");
    let Ok(entries) = std::fs::read_dir(&src) else {
        return false;
    };
    entries
        .filter_map(Result::ok)
        .filter(|entry| entry.path().extension().is_some_and(|ext| ext == "rs"))
        .any(|entry| {
            let text = std::fs::read_to_string(entry.path()).unwrap_or_default();
            text.lines()
                .map(|line| line.split("//").next().unwrap_or(""))
                .any(|code| {
                    let code = code.trim();
                    (code.starts_with("pub fn ") || code.starts_with("pub async fn "))
                        && ["publish", "upload"]
                            .iter()
                            .any(|verb| code.contains(&format!("fn {verb}")))
                })
        })
}

fn implemented_destinations() -> Vec<&'static DestinationContentProfile> {
    DESTINATION_CONTENT_PROFILES
        .iter()
        .filter(|profile| !profile.implemented.is_empty())
        .collect()
}

#[test]
fn every_implemented_destination_has_a_provider_crate_in_the_workspace() {
    let mut checked = 0usize;
    for profile in implemented_destinations() {
        let platform = profile.platform;
        let krate = provider_crate(platform).unwrap_or_else(|| {
            panic!(
                "{platform} lists implemented content types ({:?}) but names no provider crate: \
                 a destination is only implemented if a crate in this workspace can publish to it",
                profile.implemented
            )
        });
        assert!(
            workspace_root().join("crates").join(krate).is_dir(),
            "{platform} claims -> {krate}, which does not exist under crates/"
        );
        assert!(
            is_workspace_member(krate),
            "{platform} claims -> {krate}, which is not a member of the Cargo workspace"
        );
        assert!(
            has_publish_path(krate),
            "{platform} claims -> {krate}, but that crate has no public publishing entry point \
             (it is a stub), so nothing there can publish to {platform}"
        );
        checked += 1;
    }
    assert_eq!(
        checked,
        implemented_destinations().len(),
        "every implemented destination was checked"
    );
    assert!(checked > 0, "the table must implement something");
}

#[test]
fn the_provider_table_covers_every_declared_destination_exactly_once() {
    let mut mapped: Vec<&str> = DESTINATION_PROVIDER_CRATES
        .iter()
        .map(|(platform, _)| *platform)
        .collect();
    let mut declared: Vec<&str> = DESTINATION_CONTENT_PROFILES
        .iter()
        .map(|profile| profile.platform)
        .collect();
    mapped.sort_unstable();
    declared.sort_unstable();
    assert_eq!(
        mapped, declared,
        "every destination needs exactly one provider-crate entry, including the ones with none"
    );
}

#[test]
fn an_unimplemented_destination_claims_nothing() {
    for profile in DESTINATION_CONTENT_PROFILES
        .iter()
        .filter(|profile| provider_crate(profile.platform).is_none())
    {
        assert!(
            profile.implemented.is_empty(),
            "{} has no provider crate, so it cannot list implemented content types: {:?}",
            profile.platform,
            profile.implemented
        );
        assert!(
            !profile.declared.is_empty(),
            "{} is declared-but-unimplemented, which is only meaningful if it declares something",
            profile.platform
        );
    }
}

#[test]
fn the_planner_refuses_an_unimplemented_destination_with_a_clear_code() {
    for platform in ["tiktok", "threads", "facebook", "rumble"] {
        let request = RouteRequest::new(Vec::new(), "hello from the capability audit")
            .with_content_type("text");
        let capability =
            DestinationCapability::built_in(platform, true, true).expect("declared destination");
        let outcome = plan(&request, &[capability]);
        let verdict = outcome
            .destination(platform)
            .unwrap_or_else(|| panic!("{platform} must still appear in the plan"));

        assert!(
            !verdict.accepted,
            "{platform} has no provider crate, so the plan must not accept it"
        );
        assert_eq!(
            verdict.refusals.first().map(|reason| reason.code.as_str()),
            Some(UNIMPLEMENTED_DESTINATION_CODE),
            "{platform} must be refused by its own code, not as a content-type problem: {:?}",
            verdict.refusals
        );
        assert!(
            verdict
                .refusals
                .first()
                .is_some_and(|reason| reason.message.contains("not implemented")),
            "the refusal has to say what is wrong: {:?}",
            verdict.refusals
        );
        assert!(
            !outcome.ready(),
            "{platform} alone cannot make a plan ready"
        );
        assert!(
            !outcome.accepted.contains(&platform.to_string()),
            "{platform} must not be in the accepted list"
        );
    }
}

#[test]
fn a_messaging_destination_is_still_refused_as_messaging() {
    // Messenger is unimplemented too, but the sharper refusal for a DM
    // destination is that it does not publish at all — the messaging sentence
    // must not be replaced by the implementation one.
    let request = RouteRequest::new(Vec::new(), "hello").with_content_type("text");
    let capability =
        DestinationCapability::built_in("messenger", true, true).expect("messenger capability");
    let outcome = plan(&request, &[capability]);
    let verdict = outcome.destination("messenger").expect("verdict");
    assert!(!verdict.accepted);
    assert_eq!(verdict.refusals[0].code, "content_type.unsupported");
    assert!(
        verdict.refusals[0]
            .message
            .contains("messaging destination"),
        "{:?}",
        verdict.refusals
    );
}
