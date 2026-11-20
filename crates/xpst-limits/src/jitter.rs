//! Jittered intervals — log-normal, never metronomic.
//!
//! Policy rule 2: "jitter every interval by ±30–50 %, log-normal. Metronomic
//! cadence is the clearest automation signature." Three consequences shape this
//! module:
//!
//! * **The shape is log-normal**, not uniform: the multiplier is
//!   `exp(σ·Z)` with `Z` standard normal, so intervals are multiplicatively
//!   symmetric around the nominal value with a right-hand tail — the way human
//!   spacing actually clusters. σ is half the platform's spread, which puts the
//!   documented ±30–50 % band at roughly two standard deviations.
//! * **The floor is never breached.** The nominal interval is inflated
//!   ([`Envelope::nominal_interval_ms`]) so the downward half of the band still
//!   lands at or above the platform's documented minimum interval. Jitter exists
//!   to look human, never to go under a platform limit.
//! * **The draw is deterministic for a given (platform, account, last publish)**,
//!   not random per call. Two processes asking "when may this account post
//!   next?" must get the same instant, and so must the same process asking
//!   twice — otherwise a DEFER would drift forward on every poll and a
//!   scheduler could never satisfy it. Successive publishes still differ,
//!   because the instant they key on changes.
//!
//! No RNG dependency: a hash of the key feeds a splitmix64 finaliser, which is
//! more than enough mixing for a timing offset (it is not a security boundary),
//! and it keeps the draw reproducible on every platform the crate builds for.

use crate::envelope::{Envelope, Platform};

/// The jittered minimum interval, in milliseconds, for a publish that happened
/// at `last_publish_ms`.
///
/// Always within `[env.min_interval_ms(), env.max_interval_ms()]`, and never
/// equal to the documented floor unless the clamp had to catch a tail sample.
#[must_use]
pub fn jittered_interval_ms(
    env: &Envelope,
    platform: Platform,
    account: &str,
    last_publish_ms: i64,
) -> i64 {
    let multiplier = multiplier(env, platform, account, last_publish_ms);
    let nominal = env.nominal_interval_ms() as f64;
    let sampled = (nominal * multiplier).round() as i64;
    sampled.clamp(env.min_interval_ms(), env.max_interval_ms())
}

/// The draw itself, as a multiplier of the nominal interval. Exposed so a
/// report or a test can state the multiplier that produced an interval.
#[must_use]
pub fn multiplier(env: &Envelope, platform: Platform, account: &str, last_publish_ms: i64) -> f64 {
    let key = format!("{}|{}|{}|v1", platform.as_str(), account, last_publish_ms);
    let unit = uniform(&key);
    let sigma = env.jitter_spread / 2.0;
    (sigma * inverse_normal(unit)).exp()
}

/// A uniform draw in the open interval (0, 1) derived from `key`.
fn uniform(key: &str) -> f64 {
    let mixed = splitmix64(fnv1a64(key.as_bytes()));
    // 53 significand bits -> the largest exactly representable uniform grid.
    let bits = mixed >> 11;
    let unit = bits as f64 / (1u64 << 53) as f64;
    // Keep clear of the ends so the inverse normal stays finite.
    unit.clamp(1e-9, 1.0 - 1e-9)
}

fn fnv1a64(bytes: &[u8]) -> u64 {
    let mut hash = 0xcbf2_9ce4_8422_2325u64;
    for byte in bytes {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    hash
}

fn splitmix64(state: u64) -> u64 {
    let mut z = state.wrapping_add(0x9e37_79b9_7f4a_7c15);
    z = (z ^ (z >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
    z ^ (z >> 31)
}

/// Inverse of the standard normal CDF (Acklam's rational approximation,
/// |error| < 1.2e-9). Standard library material, reproduced here so the crate
/// needs no statistics dependency.
fn inverse_normal(p: f64) -> f64 {
    const A: [f64; 6] = [
        -3.969_683_028_665_376e1,
        2.209_460_984_245_205e2,
        -2.759_285_104_469_687e2,
        1.383_577_518_672_69e2,
        -3.066_479_806_614_716e1,
        2.506_628_277_459_239,
    ];
    const B: [f64; 5] = [
        -5.447_609_879_822_406e1,
        1.615_858_368_580_409e2,
        -1.556_989_798_598_866e2,
        6.680_131_188_771_972e1,
        -1.328_068_155_288_572e1,
    ];
    const C: [f64; 6] = [
        -7.784_894_002_430_293e-3,
        -3.223_964_580_411_365e-1,
        -2.400_758_277_161_838,
        -2.549_732_539_343_734,
        4.374_664_141_464_968,
        2.938_163_982_698_783,
    ];
    const D: [f64; 4] = [
        7.784_695_709_041_462e-3,
        3.224_671_290_700_398e-1,
        2.445_134_137_142_996,
        3.754_408_661_907_416,
    ];
    const P_LOW: f64 = 0.024_25;
    const P_HIGH: f64 = 1.0 - P_LOW;

    if p < P_LOW {
        let q = (-2.0 * p.ln()).sqrt();
        (((((C[0] * q + C[1]) * q + C[2]) * q + C[3]) * q + C[4]) * q + C[5])
            / ((((D[0] * q + D[1]) * q + D[2]) * q + D[3]) * q + 1.0)
    } else if p <= P_HIGH {
        let q = p - 0.5;
        let r = q * q;
        (((((A[0] * r + A[1]) * r + A[2]) * r + A[3]) * r + A[4]) * r + A[5]) * q
            / (((((B[0] * r + B[1]) * r + B[2]) * r + B[3]) * r + B[4]) * r + 1.0)
    } else {
        let q = (-2.0 * (1.0 - p).ln()).sqrt();
        -(((((C[0] * q + C[1]) * q + C[2]) * q + C[3]) * q + C[4]) * q + C[5])
            / ((((D[0] * q + D[1]) * q + D[2]) * q + D[3]) * q + 1.0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::envelope::ENVELOPES;

    #[test]
    fn inverse_normal_matches_known_quantiles() {
        for (p, expected) in [
            (0.5, 0.0),
            (0.975, 1.959_963_984_540_054),
            (0.025, -1.959_963_984_540_054),
            (0.841_344_746_068_543, 1.0),
            (0.01, -2.326_347_874_040_841),
        ] {
            let got = inverse_normal(p);
            assert!(
                (got - expected).abs() < 1e-6,
                "inverse_normal({p}) = {got}, expected {expected}"
            );
        }
    }

    #[test]
    fn draws_are_deterministic_for_the_same_key() {
        let env = crate::envelope::envelope(Platform::X);
        let first = jittered_interval_ms(env, Platform::X, "acct-a", 1_700_000_000_000);
        let second = jittered_interval_ms(env, Platform::X, "acct-a", 1_700_000_000_000);
        assert_eq!(first, second, "a DEFER must not drift between polls");
    }

    #[test]
    fn intervals_stay_inside_the_band_and_are_not_metronomic() {
        for env in &ENVELOPES {
            let floor = env.min_interval_ms();
            let ceiling = env.max_interval_ms();
            let mut seen: Vec<i64> = Vec::new();
            let mut instant = 1_700_000_000_000;
            for step in 0..500 {
                let account = format!("acct-{step}");
                let interval = jittered_interval_ms(env, env.platform, &account, instant);
                assert!(
                    (floor..=ceiling).contains(&interval),
                    "{} produced {interval} outside [{floor}, {ceiling}]",
                    env.platform
                );
                seen.push(interval);
                instant += interval;
            }
            let at_floor = seen.iter().filter(|value| **value == floor).count();
            assert!(
                at_floor * 10 <= seen.len(),
                "{}: {at_floor} of {} intervals sat exactly on the floor — that is a \
                 metronome, not jitter",
                env.platform,
                seen.len()
            );
            let mut distinct = seen.clone();
            distinct.sort_unstable();
            distinct.dedup();
            assert!(
                distinct.len() > seen.len() * 9 / 10,
                "{}: only {} distinct intervals in {} draws",
                env.platform,
                distinct.len(),
                seen.len()
            );
            let mean = seen.iter().sum::<i64>() as f64 / seen.len() as f64;
            let variance =
                seen.iter().map(|v| (*v as f64 - mean).powi(2)).sum::<f64>() / seen.len() as f64;
            assert!(
                variance.sqrt() > 0.05 * floor as f64,
                "{}: spread {:.0} ms is too tight to look human",
                env.platform,
                variance.sqrt()
            );
        }
    }

    #[test]
    fn a_different_account_gets_a_different_interval() {
        let env = crate::envelope::envelope(Platform::Instagram);
        let a = jittered_interval_ms(env, Platform::Instagram, "acct-a", 1_700_000_000_000);
        let b = jittered_interval_ms(env, Platform::Instagram, "acct-b", 1_700_000_000_000);
        assert_ne!(a, b);
    }

    #[test]
    fn successive_publishes_do_not_repeat_an_interval() {
        let env = crate::envelope::envelope(Platform::YouTube);
        let mut instant = 1_700_000_000_000;
        let mut previous = None;
        for _ in 0..200 {
            let interval = jittered_interval_ms(env, Platform::YouTube, "acct-a", instant);
            assert_ne!(Some(interval), previous, "two identical gaps in a row");
            previous = Some(interval);
            instant += interval;
        }
    }
}
