//! Upload-time visibility: ONE registry for who can honour it, and at what level.
//!
//! Ported from `xpst/visibility.py`. `xpst post --visibility private` used to be
//! a scope decision disguised as a flag: YouTube wrote `status.privacyStatus`
//! while every other destination accepted the flag and published publicly. This
//! module is the registry that answers "which level, and can this route honour
//! it" once, for every surface — the plan payload, the preflight service, the
//! CLI advisories and the uploader's own receipt.
//!
//! Two policies, deliberately different:
//!
//! * [`Policy::Refuse`] — the destination cannot honour the request on the route
//!   that will run, and publishing anyway would silently produce something the
//!   caller did not ask for. The request is refused and nothing is published.
//! * [`Policy::Advise`] — there is no route to this destination that could honour
//!   the request (X has no per-post private mode at any layer). Refusing would
//!   mean "you cannot post to X", so the post still publishes publicly and every
//!   plan and outcome names the destination as public.
//!
//! Nothing here reads configuration or touches the network: a caller supplies
//! the TikTok route inputs and gets the registry's answer back.

/// What a caller may ask for. The CLI rejects anything else at its parser
/// (exit 2) and the uploaders refuse it by name, so an unknown value can never
/// be coerced into "public".
pub const PUBLIC: &str = "public";
pub const UNLISTED: &str = "unlisted";
pub const PRIVATE: &str = "private";
pub const VISIBILITY_CHOICES: [&str; 3] = [PUBLIC, UNLISTED, PRIVATE];

/// A destination's documented publish endpoint.
pub const ROUTE_API: &str = "api";
/// TikTok's opt-in browser-native publisher (TikTok Studio's web composer).
pub const ROUTE_TIKTOK_BROWSER: &str = "tiktok_browser";
/// TikTok's inbox-draft upload — nothing is published by xPST; a human finishes
/// the post in the app.
pub const ROUTE_TIKTOK_DRAFT: &str = "inbox_draft";
/// TikTok video published through a rented aggregator (Zernio), which owns the
/// audited TikTok app. Still the *tiktok* platform: state, quotas, content
/// limits and analytics all key on the platform name, so this is a second
/// publishing route, never a platform called "zernio".
pub const ROUTE_TIKTOK_AGGREGATOR: &str = "tiktok_aggregator";

/// Blocker code the plan/preflight emits when a requested level cannot be
/// honoured on the route that will run.
pub const VISIBILITY_UNAVAILABLE_CODE: &str = "VISIBILITY_UNAVAILABLE";
/// Warning code for a destination that will publish publicly instead (ADVISE).
pub const VISIBILITY_UNSUPPORTED_CODE: &str = "VISIBILITY_UNSUPPORTED";
/// Canonical destination state for a destination that refused the requested
/// level. It lives here because both the registry's consumers and the route plan
/// must spell it the same.
pub const REFUSED_VISIBILITY_STATE: &str = "refused_visibility";

/// How a route enforces a level it cannot honour.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Policy {
    /// Refuse the request; publish nothing.
    Refuse,
    /// Publish publicly anyway, and say so.
    Advise,
}

impl Policy {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Refuse => "refuse",
            Self::Advise => "advise",
        }
    }
}

/// What ONE destination route can do about an upload-time visibility.
#[derive(Debug, Clone, Copy)]
pub struct VisibilitySupport {
    pub platform: &'static str,
    pub route: &'static str,
    /// `(requested level, provider-native level)` — only the levels this route
    /// really honours. A request missing from this list cannot be honoured.
    pub levels: &'static [(&'static str, &'static str)],
    /// The native level this route uses when nothing was requested (`None` when
    /// the route has no such control).
    pub default_level: Option<&'static str>,
    pub policy: Policy,
    /// The sentence every surface quotes as the reason.
    pub note: &'static str,
}

impl VisibilitySupport {
    /// The provider-native level for `requested`, or `None` if unsupported.
    #[must_use]
    pub fn level_for(&self, requested: Option<&str>) -> Option<&'static str> {
        let value = normalize(requested)?;
        self.levels
            .iter()
            .find(|(level, _)| *level == value)
            .map(|(_, native)| *native)
    }

    /// The visibility levels this route honours, in canonical order.
    #[must_use]
    pub fn requestable(&self) -> Vec<&'static str> {
        VISIBILITY_CHOICES
            .iter()
            .copied()
            .filter(|level| self.levels.iter().any(|(name, _)| name == level))
            .collect()
    }
}

const YOUTUBE_API: VisibilitySupport = VisibilitySupport {
    platform: "youtube",
    route: ROUTE_API,
    levels: &[
        (PUBLIC, "public"),
        (UNLISTED, "unlisted"),
        (PRIVATE, "private"),
    ],
    default_level: Some("public"),
    policy: Policy::Refuse,
    note: "YouTube writes status.privacyStatus at upload time, so every level is honoured.",
};

const TIKTOK_API: VisibilitySupport = VisibilitySupport {
    platform: "tiktok",
    route: ROUTE_API,
    levels: &[(PUBLIC, "PUBLIC_TO_EVERYONE"), (PRIVATE, "SELF_ONLY")],
    default_level: None,
    policy: Policy::Refuse,
    note: "TikTok Direct Post writes post_info.privacy_level: private is SELF_ONLY and public is \
           PUBLIC_TO_EVERYONE. TikTok has no unlisted level, and the level is validated against \
           creator_info/query — a level the client may not use is refused, never downgraded.",
};

const TIKTOK_BROWSER: VisibilitySupport = VisibilitySupport {
    platform: "tiktok",
    route: ROUTE_TIKTOK_BROWSER,
    levels: &[(PUBLIC, "public")],
    default_level: Some("public"),
    policy: Policy::Refuse,
    note: "TikTok's browser-native route drives TikTok Studio's composer, which decides the \
           audience itself: xPST can neither read nor set it, and that route publishes publicly.",
};

const TIKTOK_DRAFT: VisibilitySupport = VisibilitySupport {
    platform: "tiktok",
    route: ROUTE_TIKTOK_DRAFT,
    levels: &[],
    default_level: None,
    policy: Policy::Refuse,
    note: "An inbox draft is finished by hand in the TikTok app, so xPST never publishes it and \
           cannot honour a visibility for it.",
};

const TIKTOK_AGGREGATOR: VisibilitySupport = VisibilitySupport {
    platform: "tiktok",
    route: ROUTE_TIKTOK_AGGREGATOR,
    // Business-app connections publish video publicly only. There is
    // deliberately no private/unlisted level here: a "post it privately" request
    // must be REFUSED, not silently turned into a public post.
    levels: &[(PUBLIC, "PUBLIC_TO_EVERYONE")],
    default_level: Some("PUBLIC_TO_EVERYONE"),
    policy: Policy::Refuse,
    note: "The rented aggregator route posts through a TikTok for Business connection, which \
           publishes video publicly only (PUBLIC_TO_EVERYONE); there is no private or unlisted \
           level on it.",
};

const NO_PRIVATE_MODE: &str =
    "it has no per-post private/unlisted mode on any route, so the post is PUBLIC there";

/// A destination with no upload-time privacy control anywhere (ADVISE).
const fn advisory_platform(platform: &'static str, note: &'static str) -> VisibilitySupport {
    VisibilitySupport {
        platform,
        route: ROUTE_API,
        levels: &[(PUBLIC, "public")],
        default_level: Some("public"),
        policy: Policy::Advise,
        note,
    }
}

const X_API: VisibilitySupport = advisory_platform("x", NO_PRIVATE_MODE);
const INSTAGRAM_API: VisibilitySupport = advisory_platform("instagram", NO_PRIVATE_MODE);
const THREADS_API: VisibilitySupport = advisory_platform("threads", NO_PRIVATE_MODE);
const FACEBOOK_API: VisibilitySupport = advisory_platform("facebook", NO_PRIVATE_MODE);
const BLUESKY_API: VisibilitySupport = advisory_platform("bluesky", NO_PRIVATE_MODE);
const RUMBLE_API: VisibilitySupport = advisory_platform(
    "rumble",
    "it hands the rendition off for a manual upload, so xPST does not publish it at all",
);

/// `(platform, route) -> what that route can honour`.
///
/// Named destinations in the publish list all appear here; a destination that
/// does not (a plugin, a future adapter) is treated as ADVISE with no levels,
/// which is the honest answer for an unknown publish path.
pub static VISIBILITY_SUPPORT: [VisibilitySupport; 11] = [
    YOUTUBE_API,
    TIKTOK_API,
    TIKTOK_BROWSER,
    TIKTOK_DRAFT,
    TIKTOK_AGGREGATOR,
    X_API,
    INSTAGRAM_API,
    THREADS_API,
    FACEBOOK_API,
    BLUESKY_API,
    RUMBLE_API,
];

/// The TikTok configuration `publish_route` reads.
///
/// Only TikTok has more than one publishing route, so this is the whole of the
/// configuration the registry needs. Every field is the raw configured string
/// (Python reads them with `str(getattr(...) or default).lower()`), so a value
/// `config set` wrote as a number arrives here as text.
#[derive(Debug, Clone, Default)]
pub struct TiktokRoute {
    pub provider: String,
    pub draft_mode: String,
    pub publish_mode: String,
}

/// Canonical spelling of a request: lower-case, empty → `None`.
///
/// `None`/`""` mean "nothing was requested" and are NOT the same as `"public"`:
/// an unrequested post keeps the destination's own default (and, on TikTok,
/// today's reactive downgrade for an unaudited client).
#[must_use]
pub fn normalize(requested: Option<&str>) -> Option<String> {
    let raw = requested.unwrap_or("").trim().to_ascii_lowercase();
    if raw.is_empty() {
        None
    } else {
        Some(raw)
    }
}

/// The refusal sentence for an unknown level, or `None` when it is valid.
#[must_use]
pub fn validate(requested: Option<&str>) -> Option<String> {
    let value = normalize(requested)?;
    if VISIBILITY_CHOICES.contains(&value.as_str()) {
        return None;
    }
    Some(format!(
        "'{value}' is not a visibility: choose one of {}.",
        VISIBILITY_CHOICES.join(", ")
    ))
}

/// The registry row for one destination route, or `None` if unknown.
#[must_use]
pub fn support(platform: &str, route: &str) -> Option<&'static VisibilitySupport> {
    let name = platform.trim().to_ascii_lowercase();
    VISIBILITY_SUPPORT
        .iter()
        .find(|row| row.platform == name && row.route == route)
}

/// The ADVISE row used for a destination the registry does not know.
///
/// `platform` is empty on purpose: the message names the caller's own
/// destination, which is the same string a known row would carry.
#[must_use]
pub fn unknown_destination_policy() -> VisibilitySupport {
    VisibilitySupport {
        platform: "",
        route: ROUTE_API,
        levels: &[(PUBLIC, "public")],
        default_level: Some("public"),
        policy: Policy::Advise,
        note: "no upload-time visibility control is declared for this destination",
    }
}

/// The route that would actually publish for this destination right now.
///
/// Only TikTok has more than one publishing route, so this is where its
/// configured `publish_mode` / `draft_mode` is translated into the route id the
/// registry keys on. Every surface asks this function instead of reading the
/// config itself: a plan that says "private" for a destination whose route
/// cannot honour it is exactly the lie the registry exists to prevent.
///
/// `requested` matters because a requested level CHANGES the route choice: Direct
/// Post is the only TikTok route that can set privacy, and an unaudited client is
/// allowed to post privately — so a request keeps the official route instead of
/// taking the browser preference, which would refuse. Without a request the
/// documented preference stands (browser first when the client is unaudited).
#[must_use]
pub fn publish_route(
    tiktok: Option<&TiktokRoute>,
    platform: &str,
    requested: Option<&str>,
) -> &'static str {
    let name = platform.trim().to_ascii_lowercase();
    if name != "tiktok" {
        return ROUTE_API;
    }
    let Some(account) = tiktok else {
        return ROUTE_API;
    };
    // A rented aggregator connection is the whole publishing lane for this
    // destination: every level it accepts must be answered from its own row, so
    // a private request is refused instead of being routed to a route that
    // cannot honour it.
    let provider = lower_or(&account.provider, "local");
    if provider == "zernio" {
        return ROUTE_TIKTOK_AGGREGATOR;
    }
    let draft = lower_or(&account.draft_mode, "auto");
    if draft == "always" {
        return ROUTE_TIKTOK_DRAFT;
    }
    let mode = lower_or(&account.publish_mode, "auto");
    if !matches!(
        mode.as_str(),
        ROUTE_TIKTOK_BROWSER | "browser" | "browser_only"
    ) {
        return ROUTE_API;
    }
    if mode == "browser_only" {
        return ROUTE_TIKTOK_BROWSER;
    }
    // "browser": Direct Post is attempted first, so a visibility request is
    // served by the official route. Only an unrequested post takes the
    // browser-first preference (for an unaudited client).
    if normalize(requested).is_some() {
        return ROUTE_API;
    }
    ROUTE_TIKTOK_BROWSER
}

fn lower_or(value: &str, fallback: &str) -> String {
    let trimmed = value.trim().to_ascii_lowercase();
    if trimmed.is_empty() {
        fallback.to_owned()
    } else {
        trimmed
    }
}

/// The sentence refusing `requested` on this route, or `None` if honoured.
///
/// `None`/`""` (nothing requested) is never refused. A level the route does not
/// honour is refused with the registry's own note, so the CLI, the plan, the MCP
/// tool and the uploader all quote one reason.
#[must_use]
pub fn refusal(platform: &str, requested: Option<&str>, route: &str) -> Option<String> {
    let value = normalize(requested)?;
    if let Some(invalid) = validate(Some(&value)) {
        return Some(invalid);
    }
    let owned;
    let row = match support(platform, route) {
        Some(row) => row,
        None => {
            owned = unknown_destination_policy();
            &owned
        }
    };
    if row.levels.iter().any(|(level, _)| *level == value) {
        return None;
    }
    if row.policy != Policy::Refuse {
        // An ADVISE destination cannot honour it on any route (X has no private
        // mode at all), so refusing would mean "you cannot post to X". It still
        // publishes, and `advisory` is what says "public" out loud.
        return None;
    }
    Some(format!(
        "{name} cannot honour --visibility {value} on this route: {note} The request is \
         refused rather than posting publicly; drop --visibility {value} (the post is public \
         there) or use a route that can honour it.",
        name = platform.trim().to_ascii_lowercase(),
        note = row.note,
    ))
}

/// The ADVISE sentence for a post that goes out publicly anyway, or `None`.
///
/// Empty for the default (nothing requested): every destination publishes
/// publicly then, so there is nothing to warn about.
#[must_use]
pub fn advisory(platform: &str, requested: Option<&str>, route: &str) -> Option<String> {
    let value = normalize(requested)?;
    let owned;
    let row = match support(platform, route) {
        Some(row) => row,
        None => {
            owned = unknown_destination_policy();
            &owned
        }
    };
    if row.policy != Policy::Advise || row.levels.iter().any(|(level, _)| *level == value) {
        return None;
    }
    Some(format!(
        "{name} cannot honour --visibility {value}: {note}. The post is PUBLIC there.",
        name = platform.trim().to_ascii_lowercase(),
        note = row.note,
    ))
}

/// The per-destination plan row for a *requested* visibility.
///
/// `None` when nothing was requested, so the default payload shape is untouched.
/// Otherwise it states the level that would be used and whether the route can
/// honour it at all — the answer a caller previously had to infer from prose.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PlanEntry {
    pub requested: String,
    pub level: Option<String>,
    pub honoured: bool,
    pub route: String,
    pub enforced: String,
    pub note: Option<String>,
}

/// Build the plan row for a requested visibility, or `None` when none was asked.
///
/// `honoured` overrides the registry's static answer for a refusal only the
/// provider could settle (the client may not use the level according to
/// `creator_info/query`). Callers pass `Some(false)` there; the row shape stays
/// this function's job.
#[must_use]
pub fn plan_entry(
    platform: &str,
    requested: Option<&str>,
    route: &str,
    level: Option<&str>,
    honoured: Option<bool>,
) -> Option<PlanEntry> {
    let value = normalize(requested)?;
    let owned;
    let row = match support(platform, route) {
        Some(row) => row,
        None => {
            owned = unknown_destination_policy();
            &owned
        }
    };
    let native = match level {
        Some(level) => Some(level.to_owned()),
        None => row.level_for(Some(&value)).map(str::to_owned),
    };
    let can_honour = honoured.unwrap_or(native.is_some());
    Some(PlanEntry {
        requested: value,
        level: native,
        honoured: can_honour,
        route: route.to_owned(),
        enforced: row.policy.as_str().to_owned(),
        note: if !can_honour || row.policy == Policy::Advise {
            Some(row.note.to_owned())
        } else {
            None
        },
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_requested_level_is_never_the_default() {
        assert_eq!(normalize(None), None);
        assert_eq!(normalize(Some("  ")), None);
        assert_eq!(normalize(Some("PRIVATE")), Some("private".to_owned()));
    }

    #[test]
    fn youtube_honours_every_level_and_x_advises() {
        for level in VISIBILITY_CHOICES {
            assert_eq!(refusal("youtube", Some(level), ROUTE_API), None);
            assert_eq!(advisory("youtube", Some(level), ROUTE_API), None);
        }
        assert_eq!(refusal("x", Some(PRIVATE), ROUTE_API), None);
        assert!(advisory("x", Some(PRIVATE), ROUTE_API).is_some());
    }

    #[test]
    fn tiktok_browser_refuses_private_and_direct_post_honours_it() {
        assert!(refusal("tiktok", Some(PRIVATE), ROUTE_TIKTOK_BROWSER).is_some());
        assert_eq!(refusal("tiktok", Some(PRIVATE), ROUTE_API), None);
        assert!(refusal("tiktok", Some(UNLISTED), ROUTE_API).is_some());
    }

    #[test]
    fn a_request_keeps_the_official_tiktok_route() {
        let browser = TiktokRoute {
            provider: "local".to_owned(),
            draft_mode: "auto".to_owned(),
            publish_mode: "browser".to_owned(),
        };
        assert_eq!(
            publish_route(Some(&browser), "tiktok", None),
            ROUTE_TIKTOK_BROWSER
        );
        assert_eq!(
            publish_route(Some(&browser), "tiktok", Some(PRIVATE)),
            ROUTE_API
        );
        assert_eq!(publish_route(None, "tiktok", None), ROUTE_API);
        assert_eq!(publish_route(None, "youtube", None), ROUTE_API);
    }

    #[test]
    fn an_unknown_destination_advises_rather_than_refusing() {
        assert_eq!(refusal("plugin", Some(PRIVATE), ROUTE_API), None);
        let note = advisory("plugin", Some(PRIVATE), ROUTE_API).expect("advisory");
        assert!(note.contains("no upload-time visibility control is declared"));
    }

    #[test]
    fn plan_entry_states_the_level_and_the_policy() {
        let entry = plan_entry("x", Some(PRIVATE), ROUTE_API, None, None).expect("entry");
        assert_eq!(entry.level, None);
        assert!(!entry.honoured);
        assert_eq!(entry.enforced, "advise");
        assert!(entry.note.is_some());

        let entry = plan_entry("tiktok", Some(PRIVATE), ROUTE_API, None, None).expect("entry");
        assert_eq!(entry.level.as_deref(), Some("SELF_ONLY"));
        assert!(entry.honoured);
        assert_eq!(entry.note, None);

        assert_eq!(plan_entry("x", None, ROUTE_API, None, None), None);
    }
}
