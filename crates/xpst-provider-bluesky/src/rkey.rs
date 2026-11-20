//! Deterministic record keys.
//!
//! `com.atproto.repo.createRecord` accepts an `rkey`. Left unset, the PDS
//! generates one — and a generated key is what makes a lost response
//! unrecoverable: the retry names a *different* record, so the client cannot ask
//! "did my write land?" and can only publish again. Supplying a key derived from
//! the content removes that: the same post always names the same record, so a
//! retry addresses the write that may already exist.
//!
//! The key has to be a **TID**, not a hash in hex. `app.bsky.feed.post` declares
//! its record key type as `tid`, and the PDS validates a supplied rkey against
//! the record's own key schema (`prepareCreate` → `validateRecord` →
//! `schema.keySchema.safeValidate`) — an arbitrary hex string is rejected with
//! "Invalid record key for app.bsky.feed.post". So the hash is *encoded as* a
//! TID: 64 bits of SHA-256, top bit cleared, in the base32-sortable alphabet.
//!
//! Two consequences worth stating rather than discovering:
//!
//! * the timestamp a TID usually carries is replaced by hash bits. The
//!   [TID spec](https://atproto.com/specs/tid) says record keys "can be specified
//!   by the end user and could have any value, so they should not be trusted" and
//!   that "implementations should not rely on global uniqueness of TIDs", so this
//!   is legal; what it gives up is the *loose temporal ordering* of a repo's
//!   posts, which is a storage nicety, not a protocol requirement.
//! * identical content maps to an identical key **by design**. Posting the same
//!   text twice is therefore one record, not two. That is the price of
//!   idempotency, and it is the right price for a cross-poster whose retry and
//!   its original must not become two posts. Callers that genuinely want two
//!   identical posts must vary the key material, which is why [`tid_from_parts`]
//!   takes the parts rather than a pre-joined string.

use sha2::{Digest, Sha256};

/// The base32-sortable alphabet the TID spec mandates, most significant digit
/// first. Index 0 is the zero digit, which is why an all-zero TID is
/// `2222222222222`.
const BASE32_SORTABLE: [u8; 32] = *b"234567abcdefghijklmnopqrstuvwxyz";

/// A record key derived from the parts of a request, as a 13-character TID.
///
/// Parts are length-prefixed before hashing, so `["ab", "c"]` and `["a", "bc"]`
/// cannot collide into the same key. The output satisfies the TID syntax the
/// lexicon's `tid` key type enforces: exactly 13 characters, all from
/// `234567abcdefghijklmnopqrstuvwxyz`, first character from `234567abcdefghij`.
pub fn tid_from_parts(parts: &[&str]) -> String {
    let mut hasher = Sha256::new();
    for part in parts {
        hasher.update(u64::try_from(part.len()).unwrap_or(u64::MAX).to_be_bytes());
        hasher.update(part.as_bytes());
    }
    let digest = hasher.finalize();

    let mut bytes = [0u8; 8];
    bytes.copy_from_slice(&digest[..8]);
    // The TID spec: "The top bit is always 0". Clearing it is also what keeps the
    // first character inside the allowed subset.
    let word = u64::from_be_bytes(bytes) & 0x7fff_ffff_ffff_ffff;

    (0..13)
        .rev()
        .map(|digit| {
            let index = usize::try_from((word >> (digit * 5)) & 0x1f).unwrap_or(0);
            BASE32_SORTABLE[index] as char
        })
        .collect()
}

/// Whether `key` is a syntactically valid TID, as the lexicon `tid` format is
/// specified: 13 characters, base32-sortable, and a first character that cannot
/// set the high bit.
pub fn is_tid(key: &str) -> bool {
    if key.len() != 13 {
        return false;
    }
    let mut characters = key.bytes();
    let Some(first) = characters.next() else {
        return false;
    };
    BASE32_SORTABLE[..16].contains(&first) && characters.all(|byte| BASE32_SORTABLE.contains(&byte))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_derived_key_is_a_tid_the_lexicon_will_accept() {
        let key = tid_from_parts(&["app.bsky.feed.post", "hello"]);
        assert_eq!(key.len(), 13, "a TID is always 13 characters: {key}");
        assert!(is_tid(&key), "{key} must satisfy the TID syntax");
        assert!(
            key.starts_with(|first: char| "234567abcdefghij".contains(first)),
            "the first character may not set the high bit: {key}"
        );
    }

    #[test]
    fn the_same_parts_always_produce_the_same_key() {
        let first = tid_from_parts(&["app.bsky.feed.post", "stable text", "blobcid"]);
        let second = tid_from_parts(&["app.bsky.feed.post", "stable text", "blobcid"]);
        assert_eq!(first, second);
    }

    #[test]
    fn different_parts_produce_different_keys() {
        let base = tid_from_parts(&["app.bsky.feed.post", "one"]);
        assert_ne!(base, tid_from_parts(&["app.bsky.feed.post", "two"]));
        assert_ne!(base, tid_from_parts(&["app.bsky.feed.post", "one", ""]));
    }

    #[test]
    fn part_boundaries_are_not_ambiguous() {
        // Concatenating the parts would make these two the same key.
        assert_ne!(tid_from_parts(&["ab", "c"]), tid_from_parts(&["a", "bc"]),);
    }

    #[test]
    fn even_an_empty_request_derives_a_valid_key() {
        let key = tid_from_parts(&[]);
        assert_eq!(key.len(), 13);
        assert!(is_tid(&key), "{key}");
    }

    #[test]
    fn is_tid_rejects_the_shapes_the_spec_calls_invalid() {
        assert!(!is_tid("3jzfcijpj2z21"), "1 is not in the alphabet");
        assert!(!is_tid("3JZFCIJPJ2Z2A"), "TIDs are lower case");
        assert!(!is_tid("3jzfcijpj2z2aa"), "too long");
        assert!(!is_tid("3jzfcijpj2z2"), "too short");
        assert!(!is_tid("zzzzzzzzzzzzz"), "the high bit cannot be set");
        assert!(is_tid("3jzfcijpj2z2a"), "the spec's own example must pass");
    }
}
