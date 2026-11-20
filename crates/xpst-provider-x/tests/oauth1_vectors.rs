//! OAuth 1.0a signatures pinned against an independent implementation.
//!
//! `scripts/capture_x_oauth1_vectors.py` computes each signature with Python's
//! `hmac`/`hashlib` directly from RFC 5849 — it shares no code with this crate —
//! and the committed fixture is its output. This test asserts the full header
//! string, so parameter sorting, percent-encoding and base64 are all covered by
//! one comparison per case.
//!
//! Regenerate with `python3 scripts/capture_x_oauth1_vectors.py`; a diff means
//! the fixture was hand-edited or the signer changed.

use serde_json::Value;
use xpst_provider_x::oauth1::OAuth1Signer;
use xpst_provider_x::transport::Method;
use xpst_provider_x::Credentials;

fn fixture() -> Value {
    let raw = include_str!("fixtures/oauth1_vectors.json");
    serde_json::from_str(raw).expect("oauth1 fixture is valid JSON")
}

#[test]
fn signatures_match_the_python_rfc5849_reference() {
    let fixture = fixture();
    let cases = fixture["cases"].as_array().expect("cases array");
    assert_eq!(cases.len(), 4, "fixture case count changed");

    for case in cases {
        let name = case["name"].as_str().unwrap_or("unnamed");
        let credentials = &case["credentials"];
        let signer = OAuth1Signer::new(Credentials::new(
            credentials["consumer_key"].as_str().unwrap(),
            credentials["consumer_secret"].as_str().unwrap(),
            credentials["access_token"].as_str().unwrap(),
            credentials["access_token_secret"].as_str().unwrap(),
        ));
        let body_params: Vec<(String, String)> = case["body_params"]
            .as_array()
            .expect("body_params array")
            .iter()
            .map(|pair| {
                (
                    pair[0].as_str().unwrap().to_string(),
                    pair[1].as_str().unwrap().to_string(),
                )
            })
            .collect();
        let nonce = case["nonce"].as_str().unwrap();
        let timestamp = case["timestamp"].as_i64().unwrap();
        let url = case["url"].as_str().unwrap();

        let header = signer.authorization_header(Method::Post, url, &body_params, nonce, timestamp);
        assert_eq!(
            header,
            case["expected_header"].as_str().unwrap(),
            "header mismatch for {name}"
        );

        let mut params = vec![
            (
                "oauth_consumer_key".to_string(),
                credentials["consumer_key"].as_str().unwrap().to_string(),
            ),
            ("oauth_nonce".to_string(), nonce.to_string()),
            (
                "oauth_signature_method".to_string(),
                "HMAC-SHA1".to_string(),
            ),
            ("oauth_timestamp".to_string(), timestamp.to_string()),
            (
                "oauth_token".to_string(),
                credentials["access_token"].as_str().unwrap().to_string(),
            ),
            ("oauth_version".to_string(), "1.0".to_string()),
        ];
        params.extend(body_params);
        assert_eq!(
            signer.signature(Method::Post, url, &params),
            case["expected_signature"].as_str().unwrap(),
            "signature mismatch for {name}"
        );
    }
}

#[test]
fn nonces_do_not_repeat_within_a_process() {
    let first = xpst_provider_x::oauth1::fresh_nonce();
    let second = xpst_provider_x::oauth1::fresh_nonce();
    assert_ne!(first, second);
}
