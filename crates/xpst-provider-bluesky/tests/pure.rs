//! Pure helpers: AT-URI parsing, service normalisation and the facet scanner.
//!
//! None of these touch a socket, and all of them decide whether a later call is
//! allowed to make a claim about the platform.

use xpst_provider_bluesky::atrium::normalize_service;
use xpst_provider_bluesky::facets::link_facets;
use xpst_provider_bluesky::AtUri;

#[test]
fn an_at_uri_splits_into_repo_collection_and_key() {
    let uri = AtUri::parse("at://did:plc:abc123/app.bsky.feed.post/3kxyz").expect("valid AT-URI");

    assert_eq!(uri.repo, "did:plc:abc123");
    assert_eq!(uri.collection, "app.bsky.feed.post");
    assert_eq!(uri.rkey, "3kxyz");
    assert_eq!(uri.to_uri(), "at://did:plc:abc123/app.bsky.feed.post/3kxyz");
}

#[test]
fn a_web_url_or_a_bare_key_is_not_an_at_uri() {
    assert!(AtUri::parse("https://bsky.app/profile/x/post/3kxyz").is_none());
    assert!(AtUri::parse("3kxyz").is_none());
    assert!(AtUri::parse("at://").is_none());
    assert!(AtUri::parse("at://did:plc:abc123").is_none());
    assert!(AtUri::parse("at:///app.bsky.feed.post/3kxyz").is_none());
}

#[test]
fn the_service_is_normalised_to_a_scheme_and_host() {
    assert_eq!(normalize_service("bsky.social"), "https://bsky.social");
    assert_eq!(
        normalize_service("https://bsky.social/"),
        "https://bsky.social"
    );
    assert_eq!(
        normalize_service("  https://pds.example/  "),
        "https://pds.example"
    );
    assert_eq!(normalize_service(""), "https://bsky.social");
    assert_eq!(
        normalize_service("http://127.0.0.1:2583"),
        "http://127.0.0.1:2583"
    );
}

#[test]
fn links_are_faceted_with_trailing_punctuation_dropped() {
    let facets = link_facets("read https://example.com/a, then https://example.org/b.");

    assert_eq!(facets.len(), 2);
    assert_eq!(facets[0].uri, "https://example.com/a");
    assert_eq!(facets[1].uri, "https://example.org/b");
    assert_eq!(
        &"read https://example.com/a, then https://example.org/b."
            [facets[0].byte_start..facets[0].byte_end],
        "https://example.com/a"
    );
}

#[test]
fn facet_offsets_are_bytes_not_characters() {
    let text = "héllo https://example.com";
    let facets = link_facets(text);

    assert_eq!(facets.len(), 1);
    assert_eq!(
        &text[facets[0].byte_start..facets[0].byte_end],
        "https://example.com"
    );
    assert_ne!(facets[0].byte_start, 6, "6 would be the character offset");
}

#[test]
fn a_scheme_without_a_host_is_not_a_link() {
    assert!(link_facets("https:// and nothing").is_empty());
    assert!(link_facets("plain text, no links").is_empty());
    assert!(link_facets("").is_empty());
}

#[test]
fn uppercase_schemes_are_still_links() {
    let facets = link_facets("HTTPS://Example.com/Path");

    assert_eq!(facets.len(), 1);
    assert_eq!(facets[0].uri, "HTTPS://Example.com/Path");
}
