//! Exit codes are part of the public contract: `docs/TUTORIAL_CLI.md#exit-codes-reference`
//! documents them and scripts branch on them. They are pinned here once and
//! never inlined, so the Rust engine cannot drift from the documented table.

/// Success.
pub const SUCCESS: i32 = 0;
/// General error.
pub const GENERAL: i32 = 1;
/// Configuration error. Also the code for command-line usage errors, which is
/// what Click exits with for a bad invocation.
pub const CONFIG_ERROR: i32 = 2;
/// Authentication failure.
pub const AUTH_FAILURE: i32 = 3;
/// Rate limit / quota exceeded.
pub const RATE_LIMIT: i32 = 4;
/// Platform unavailable, or nothing was attemptable.
pub const PLATFORM_UNAVAILABLE: i32 = 10;

/// Code the binary exits with for a command the Rust engine does not implement
/// yet. `GENERAL` is the documented "general error" slot — a missing
/// implementation is not a config error and not an auth failure, and scripts
/// must not read it as success.
pub const NOT_IMPLEMENTED: i32 = GENERAL;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn codes_match_the_documented_table() {
        // docs/TUTORIAL_CLI.md#exit-codes-reference
        assert_eq!(SUCCESS, 0);
        assert_eq!(GENERAL, 1);
        assert_eq!(CONFIG_ERROR, 2);
        assert_eq!(AUTH_FAILURE, 3);
        assert_eq!(RATE_LIMIT, 4);
        assert_eq!(PLATFORM_UNAVAILABLE, 10);
    }
}
