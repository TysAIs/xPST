//! Base64url without padding, as RFC 7636 and RFC 4648 §5 require.

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine as _;

pub(crate) fn b64url_encode(bytes: &[u8]) -> String {
    URL_SAFE_NO_PAD.encode(bytes)
}

pub(crate) fn b64url_decode(value: &str) -> Option<Vec<u8>> {
    URL_SAFE_NO_PAD.decode(value).ok()
}

#[cfg(test)]
mod tests {
    use super::{b64url_decode, b64url_encode};

    #[test]
    fn round_trip() {
        let raw = [0_u8, 1, 2, 250, 251, 255];
        let encoded = b64url_encode(&raw);
        assert_eq!(b64url_decode(&encoded), Some(raw.to_vec()));
        assert!(!encoded.contains('='));
    }
}
