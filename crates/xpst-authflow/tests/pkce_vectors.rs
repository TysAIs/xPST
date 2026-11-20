//! PKCE conformance, including the RFC 7636 Appendix B vector.

use xpst_authflow::{s256_challenge, AuthFlowError, Pkce, MAX_VERIFIER_LEN, MIN_VERIFIER_LEN};

#[test]
fn rfc7636_appendix_b_vector() {
    // RFC 7636 Appendix B, verbatim:
    //   code_verifier  = dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk
    //   code_challenge = E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM
    let pkce = Pkce::from_verifier("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
        .expect("the RFC verifier must validate");
    assert_eq!(
        pkce.challenge(),
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    );
    assert_eq!(pkce.method(), "S256");
    assert_eq!(
        pkce.verifier().expose(),
        "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    );
}

#[test]
fn generated_verifiers_are_rfc7636_shaped_and_unique() {
    let mut seen = std::collections::BTreeSet::new();
    for _ in 0..32 {
        let pkce = Pkce::generate().expect("OS randomness");
        let verifier = pkce.verifier().expose();
        assert!((MIN_VERIFIER_LEN..=MAX_VERIFIER_LEN).contains(&verifier.len()));
        assert!(verifier
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.' | b'_' | b'~')));
        assert_eq!(pkce.challenge(), s256_challenge(verifier));
        assert_eq!(pkce.challenge().len(), 43);
        assert!(!pkce.challenge().contains('='));
        assert!(seen.insert(verifier.to_string()), "verifier repeated");
    }
}

#[test]
fn verifier_length_bounds_are_enforced() {
    let too_short = "a".repeat(MIN_VERIFIER_LEN - 1);
    let too_long = "a".repeat(MAX_VERIFIER_LEN + 1);
    assert!(matches!(
        Pkce::from_verifier(too_short),
        Err(AuthFlowError::PkceVerifierLength { .. })
    ));
    assert!(matches!(
        Pkce::from_verifier(too_long),
        Err(AuthFlowError::PkceVerifierLength { .. })
    ));
    assert!(Pkce::from_verifier("a".repeat(MIN_VERIFIER_LEN)).is_ok());
    assert!(Pkce::from_verifier("a".repeat(MAX_VERIFIER_LEN)).is_ok());
}

#[test]
fn verifier_charset_is_enforced() {
    let mut verifier = "a".repeat(MIN_VERIFIER_LEN);
    verifier.push('!');
    assert!(matches!(
        Pkce::from_verifier(verifier),
        Err(AuthFlowError::PkceVerifierCharset('!'))
    ));
}

#[test]
fn tampering_with_the_verifier_changes_the_challenge() {
    let base = "a".repeat(MIN_VERIFIER_LEN);
    let mut tampered = base.clone();
    tampered.pop();
    tampered.push('b');
    assert_ne!(s256_challenge(&base), s256_challenge(&tampered));
}
