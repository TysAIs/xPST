//! What Meta's answer actually means.
//!
//! Every failure this crate reports is one of the outcomes below. That is the
//! point: "the request failed" is not an error message a person or an agent can
//! act on, and several of Meta's failures are *not* errors in the obvious sense
//! — a login wall is served with HTTP 200 and an HTML body, and a throttle is
//! sometimes only visible as a numeric error code.
//!
//! The classification order matters and is deliberate:
//!
//! 1. **HTML body.** Instagram serves its login wall as `text/html` with status
//!    200. Parsing it as JSON produces "expected value at line 1 column 1",
//!    which reads like a malformed response instead of "your session is not
//!    accepted". Evidence for this is in the Python engine's read path
//!    (`classify_instagram_fetch_error`), captured live against
//!    `www.instagram.com/api/v1/media/<id>/info/` answering HTML with status 200.
//! 2. **Stale query body.** A rejected hard-coded GraphQL `doc_id` answers
//!    `Missing 'data' … Summary: 'Query Error'`. It must be checked before the
//!    login-wall markers because its text can also contain `Query Error`, and
//!    telling a user to re-authenticate for a provider-constant drift wastes
//!    their time.
//! 3. **Challenge.** `ChallengeRequired` / `checkpoint_required` means the
//!    account is being asked to prove it is human. It must not be folded into
//!    "rate limited": the fix is a human in a browser, and retrying deepens it.
//! 4. **Please wait a few minutes.** Instagram's own "come back shortly" answer.
//!    Distinct from a 429 with a `Retry-After`: there is no declared window.
//! 5. **Feedback required.** The account must resolve something in the app
//!    (a policy notice, a checkpoint) before it can publish again.
//! 6. **Rate limit.** HTTP 429, a `Retry-After` header, or one of Meta's
//!    throttle codes (`4`, `17`, `32`, `613`) — the last is the only signal on
//!    some Graph publishing throttles, where no wording says "rate limit".
//! 7. Everything else, each with its own name rather than a generic failure.

use serde_json::Value;

use crate::transport::HttpResponse;

/// Meta error codes that mean "you are asking too often".
///
/// Taken from the Python engine's `_META_THROTTLE_CODES`: on the Graph
/// publishing path these arrive as HTTP 429 with no rate-limit wording at all,
/// so the numeric code is the only reliable signal.
pub const META_THROTTLE_CODES: [i64; 4] = [4, 17, 32, 613];

/// How much of a provider body is kept in a message.
const DETAIL_LIMIT: usize = 400;

/// The classified meaning of one provider answer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ResponseOutcome {
    /// 2xx and the body is a JSON payload without an error object.
    Success,
    /// Meta asked us to slow down, with whatever window it declared.
    RateLimited {
        retry_after_seconds: Option<u64>,
        detail: String,
    },
    /// The account has something to resolve in the app before it can publish.
    FeedbackRequired { detail: String },
    /// "Please wait a few minutes before you try again" — no declared window.
    PleaseWaitFewMinutes { detail: String },
    /// A human has to clear a challenge/checkpoint; retrying makes it worse.
    ChallengeRequired { detail: String },
    /// An HTML page where an API payload was expected (the login wall).
    LoginWall { detail: String },
    /// An error payload that does not describe this request (provider constant
    /// drift, e.g. a rejected GraphQL `doc_id`).
    StaleErrorBody { detail: String },
    /// The token or session was refused.
    AuthRejected { detail: String },
    /// The container/media does not exist (or is not visible to this token).
    NotFound { detail: String },
    /// Meta answered 5xx.
    ServerError { status: u16, detail: String },
    /// A 2xx with a body that is neither JSON nor HTML.
    MalformedBody { detail: String },
    /// A well-formed Meta error that no rule above claimed.
    ApiError {
        code: Option<i64>,
        subcode: Option<i64>,
        detail: String,
    },
    /// A non-2xx answer with no JSON error envelope and no HTML.
    UnexpectedStatus { status: u16, detail: String },
}

impl ResponseOutcome {
    /// Whether this outcome means the request was answered successfully.
    #[must_use]
    pub fn is_success(&self) -> bool {
        matches!(self, Self::Success)
    }

    /// Stable machine label, used in logs and in [`crate::PublishError`]'s text.
    #[must_use]
    pub const fn label(&self) -> &'static str {
        match self {
            Self::Success => "ok",
            Self::RateLimited { .. } => "rate_limited",
            Self::FeedbackRequired { .. } => "feedback_required",
            Self::PleaseWaitFewMinutes { .. } => "please_wait_few_minutes",
            Self::ChallengeRequired { .. } => "challenge_required",
            Self::LoginWall { .. } => "login_wall",
            Self::StaleErrorBody { .. } => "stale_error_body",
            Self::AuthRejected { .. } => "auth_rejected",
            Self::NotFound { .. } => "not_found",
            Self::ServerError { .. } => "server_error",
            Self::MalformedBody { .. } => "malformed_body",
            Self::ApiError { .. } => "api_error",
            Self::UnexpectedStatus { .. } => "unexpected_status",
        }
    }

    /// The provider text that produced this outcome, when there is one.
    #[must_use]
    pub fn detail(&self) -> Option<&str> {
        match self {
            Self::Success => None,
            Self::RateLimited { detail, .. }
            | Self::FeedbackRequired { detail }
            | Self::PleaseWaitFewMinutes { detail }
            | Self::ChallengeRequired { detail }
            | Self::LoginWall { detail }
            | Self::StaleErrorBody { detail }
            | Self::AuthRejected { detail }
            | Self::NotFound { detail }
            | Self::MalformedBody { detail }
            | Self::ApiError { detail, .. } => Some(detail),
            Self::ServerError { detail, .. } | Self::UnexpectedStatus { detail, .. } => {
                Some(detail)
            }
        }
    }

    /// One sentence a person can act on.
    ///
    /// This is not a log line: it is what a refusal or a failure tells the user,
    /// so it names the fix where there is one.
    #[must_use]
    pub fn user_message(&self) -> String {
        match self {
            Self::Success => "Instagram accepted the request.".to_string(),
            Self::RateLimited {
                retry_after_seconds,
                ..
            } => match retry_after_seconds {
                Some(seconds) => format!(
                    "Instagram is throttling this account and asked for a {seconds}s pause. \
                     Wait it out — retrying sooner extends the window."
                ),
                None => "Instagram is throttling this account. Wait before retrying; repeated \
                         attempts extend the window."
                    .to_string(),
            },
            Self::FeedbackRequired { .. } => "Instagram wants something resolved on the account \
                (a policy notice or a checkpoint) before it will publish again. Open the \
                Instagram app, clear the prompt, then retry."
                .to_string(),
            Self::PleaseWaitFewMinutes { .. } => "Instagram asked for a few minutes before the \
                next publish. Nothing is broken; wait and retry."
                .to_string(),
            Self::ChallengeRequired { .. } => "Instagram is challenging this account (\"It was \
                me\" / checkpoint). Open instagram.com or the app, clear the challenge, then \
                retry. Repeated automated retries make the challenge worse."
                .to_string(),
            Self::LoginWall { .. } => "Instagram answered with a web login page instead of an \
                API response, so this session is not accepted. Re-authenticate and retry."
                .to_string(),
            Self::StaleErrorBody { .. } => "Instagram rejected a query that this client builds \
                from a provider constant that has since changed. Update the client; retrying \
                the same request cannot succeed."
                .to_string(),
            Self::AuthRejected { .. } => "Instagram refused the access token. Re-authenticate \
                and retry."
                .to_string(),
            Self::NotFound { .. } => "Instagram does not know that container or media id for \
                this account."
                .to_string(),
            Self::ServerError { status, .. } => {
                format!("Instagram returned HTTP {status}. That is Meta's side; retry later.")
            }
            Self::MalformedBody { .. } => "Instagram returned a 2xx response that is not JSON. \
                The request reached something that is not the publishing API."
                .to_string(),
            Self::ApiError { code, .. } => match code {
                Some(code) => format!("Instagram rejected the request with error code {code}."),
                None => "Instagram rejected the request.".to_string(),
            },
            Self::UnexpectedStatus { status, .. } => {
                format!("Instagram answered HTTP {status} with no API error payload.")
            }
        }
    }
}

/// Classify one provider answer.
#[must_use]
pub fn classify(response: &HttpResponse) -> ResponseOutcome {
    let body = response.body.as_str();

    if looks_like_html(body) {
        // A login wall is served with 200 (and sometimes 403). An HTML body on a
        // 5xx is a proxy or an error page, not a session problem, so it keeps
        // its status.
        if response.is_success() || response.status == 403 {
            return ResponseOutcome::LoginWall { detail: head(body) };
        }
        return ResponseOutcome::ServerError {
            status: response.status,
            detail: head(body),
        };
    }

    let error = error_object(body);

    if let Some(error) = &error {
        let message = string_field(error, "message");
        let code = i64_field(error, "code");
        let subcode = i64_field(error, "error_subcode");
        let haystack = searchable(error);

        if is_stale_query(&haystack) {
            return ResponseOutcome::StaleErrorBody {
                detail: head(&message),
            };
        }
        if contains_any(&haystack, &CHALLENGE_MARKERS) {
            return ResponseOutcome::ChallengeRequired {
                detail: head(&message),
            };
        }
        if contains_any(&haystack, &PLEASE_WAIT_MARKERS) {
            return ResponseOutcome::PleaseWaitFewMinutes {
                detail: head(&message),
            };
        }
        if contains_any(&haystack, &FEEDBACK_MARKERS) {
            return ResponseOutcome::FeedbackRequired {
                detail: head(&message),
            };
        }
        if response.status == 429
            || code.is_some_and(|code| META_THROTTLE_CODES.contains(&code))
            || contains_any(&haystack, &RATE_LIMIT_MARKERS)
        {
            return ResponseOutcome::RateLimited {
                retry_after_seconds: retry_after(response, &haystack),
                detail: head(&message),
            };
        }
        if response.status == 401 || code == Some(190) || contains_any(&haystack, &AUTH_MARKERS) {
            return ResponseOutcome::AuthRejected {
                detail: head(&message),
            };
        }
        if response.status == 404 || code == Some(803) {
            return ResponseOutcome::NotFound {
                detail: head(&message),
            };
        }
        return ResponseOutcome::ApiError {
            code,
            subcode,
            detail: head(&message),
        };
    }

    if response.is_success() {
        if serde_json::from_str::<Value>(body).is_ok() {
            return ResponseOutcome::Success;
        }
        return ResponseOutcome::MalformedBody { detail: head(body) };
    }

    match response.status {
        429 => ResponseOutcome::RateLimited {
            retry_after_seconds: retry_after(response, ""),
            detail: head(body),
        },
        401 | 403 => ResponseOutcome::AuthRejected { detail: head(body) },
        404 => ResponseOutcome::NotFound { detail: head(body) },
        status if (500..600).contains(&status) => ResponseOutcome::ServerError {
            status,
            detail: head(body),
        },
        status => ResponseOutcome::UnexpectedStatus {
            status,
            detail: head(body),
        },
    }
}

/// Whether a body is an HTML document rather than an API payload.
///
/// Only the head is examined: a JSON body that happens to contain `<html>` in a
/// caption must not be mistaken for a login page.
#[must_use]
pub fn looks_like_html(body: &str) -> bool {
    let head: String = body.trim_start().chars().take(512).collect();
    let head = head.to_ascii_lowercase();
    head.starts_with("<!doctype") || head.starts_with("<html")
}

/// Session/challenge wording Instagram uses on the web path.
const CHALLENGE_MARKERS: [&str; 4] = [
    "challenge_required",
    "challenge required",
    "checkpoint_required",
    "checkpoint required",
];

/// "Come back shortly" — no declared window, so it is not a 429.
const PLEASE_WAIT_MARKERS: [&str; 2] = ["please wait a few minutes", "wait a few minutes"];

/// The account must resolve something in the app.
const FEEDBACK_MARKERS: [&str; 3] = ["feedback_required", "feedback required", "feedbackrequired"];

/// Providers that say "rate limit" in words.
const RATE_LIMIT_MARKERS: [&str; 3] = ["too many requests", "rate limit", "rate-limit"];

/// Token/session refusal wording (used only when no error code is present).
const AUTH_MARKERS: [&str; 4] = [
    "access token",
    "session has expired",
    "session is invalid",
    "login_required",
];

/// The error object of a Graph payload, if the payload is one.
fn error_object(body: &str) -> Option<Value> {
    let parsed: Value = serde_json::from_str(body).ok()?;
    let error = parsed.get("error")?;
    error.is_object().then(|| error.clone())
}

/// Everything in an error object, lowercased, for marker matching.
///
/// Meta puts the real wording in different fields per product (`message`,
/// `type`, `error_user_msg`, `error_data`), so matching only `message` misses
/// cases the platform clearly names.
fn searchable(error: &Value) -> String {
    let mut buffer = String::new();
    collect_strings(error, &mut buffer);
    buffer.to_ascii_lowercase()
}

fn collect_strings(value: &Value, buffer: &mut String) {
    match value {
        Value::String(text) => {
            buffer.push_str(text);
            buffer.push(' ');
        }
        Value::Object(map) => {
            for (key, value) in map {
                buffer.push_str(key);
                buffer.push(' ');
                collect_strings(value, buffer);
            }
        }
        Value::Array(items) => {
            for item in items {
                collect_strings(item, buffer);
            }
        }
        _ => {}
    }
}

fn string_field(error: &Value, field: &str) -> String {
    error
        .get(field)
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string()
}

fn i64_field(error: &Value, field: &str) -> Option<i64> {
    error.get(field).and_then(Value::as_i64)
}

fn contains_any(haystack: &str, needles: &[&str]) -> bool {
    needles.iter().any(|needle| haystack.contains(needle))
}

/// A rejected hard-coded GraphQL `doc_id`: an error body describing a query the
/// provider no longer serves, not a failure of this request.
fn is_stale_query(haystack: &str) -> bool {
    haystack.contains("doc_id")
        && (haystack.contains("query error") || haystack.contains("missing 'data'"))
}

/// The declared pause, from the header first and the body second.
fn retry_after(response: &HttpResponse, haystack: &str) -> Option<u64> {
    if let Some(header) = response.header("retry-after") {
        if let Ok(seconds) = header.trim().parse::<u64>() {
            return Some(seconds);
        }
        // Meta sometimes sends an HTTP date; an unparseable hint is better
        // reported as absent than guessed at.
        return None;
    }
    parse_retry_after_text(haystack)
}

/// `Retry-After: 900` / `retry after 900` inside a body or message.
fn parse_retry_after_text(text: &str) -> Option<u64> {
    let lowered = text.to_ascii_lowercase();
    let marker = lowered
        .find("retry-after")
        .or_else(|| lowered.find("retry after"))?;
    let rest = &lowered[marker..];
    let digits: String = rest
        .chars()
        .skip_while(|ch| !ch.is_ascii_digit())
        .take_while(char::is_ascii_digit)
        .collect();
    digits.parse().ok()
}

/// Bounded, single-line excerpt of a provider body.
pub(crate) fn head(body: &str) -> String {
    let cleaned = body.split_whitespace().collect::<Vec<_>>().join(" ");
    let mut excerpt: String = cleaned.chars().take(DETAIL_LIMIT).collect();
    if cleaned.chars().count() > DETAIL_LIMIT {
        excerpt.push('…');
    }
    excerpt
}

#[cfg(test)]
mod tests {
    use super::*;

    fn response(status: u16, headers: Vec<(&str, &str)>, body: &str) -> HttpResponse {
        HttpResponse::new(
            status,
            headers
                .into_iter()
                .map(|(name, value)| (name.to_string(), value.to_string()))
                .collect(),
            body,
        )
    }

    #[test]
    fn html_on_200_is_a_login_wall() {
        let outcome = classify(&response(
            200,
            Vec::new(),
            "<!DOCTYPE html><html><head><title>Login • Instagram</title></head></html>",
        ));
        assert!(matches!(outcome, ResponseOutcome::LoginWall { .. }));
        assert!(outcome.user_message().contains("login page"));
    }

    #[test]
    fn a_json_body_mentioning_html_is_not_a_login_wall() {
        let outcome = classify(&response(200, Vec::new(), r#"{"id":"17895695668004550"}"#));
        assert_eq!(outcome, ResponseOutcome::Success);
    }

    #[test]
    fn throttle_by_numeric_code_only() {
        let outcome = classify(&response(
            429,
            Vec::new(),
            r#"{"error":{"message":"","type":"OAuthException","code":4,"fbtrace_id":"abc"}}"#,
        ));
        assert!(matches!(
            outcome,
            ResponseOutcome::RateLimited {
                retry_after_seconds: None,
                ..
            }
        ));
    }

    #[test]
    fn retry_after_header_is_carried() {
        let outcome = classify(&response(
            429,
            vec![("Retry-After", "900")],
            r#"{"error":{"message":"Application request limit reached","code":4}}"#,
        ));
        match outcome {
            ResponseOutcome::RateLimited {
                retry_after_seconds,
                ..
            } => assert_eq!(retry_after_seconds, Some(900)),
            other => panic!("expected a rate limit, got {other:?}"),
        }
    }

    #[test]
    fn challenge_beats_rate_limit_wording() {
        let outcome = classify(&response(
            400,
            Vec::new(),
            r#"{"error":{"message":"challenge_required: too many requests","code":-1}}"#,
        ));
        assert!(matches!(outcome, ResponseOutcome::ChallengeRequired { .. }));
    }

    #[test]
    fn please_wait_and_feedback_are_separate() {
        let wait = classify(&response(
            400,
            Vec::new(),
            r#"{"error":{"message":"Please wait a few minutes before you try again.","code":-1}}"#,
        ));
        assert!(matches!(wait, ResponseOutcome::PleaseWaitFewMinutes { .. }));

        let feedback = classify(&response(
            400,
            Vec::new(),
            r#"{"error":{"message":"Feedback required","type":"IGApiException","code":-1}}"#,
        ));
        assert!(matches!(feedback, ResponseOutcome::FeedbackRequired { .. }));
    }

    #[test]
    fn stale_query_body_is_its_own_outcome() {
        let outcome = classify(&response(
            400,
            Vec::new(),
            r#"{"error":{"message":"Missing 'data' in doc_id GraphQL response. Summary: 'Query Error'","code":-1}}"#,
        ));
        assert!(matches!(outcome, ResponseOutcome::StaleErrorBody { .. }));
    }

    #[test]
    fn expired_token_is_auth_rejected() {
        let outcome = classify(&response(
            400,
            Vec::new(),
            r#"{"error":{"message":"Invalid OAuth access token","type":"OAuthException","code":190}}"#,
        ));
        assert!(matches!(outcome, ResponseOutcome::AuthRejected { .. }));
    }

    #[test]
    fn a_2xx_that_is_not_json_is_malformed_not_success() {
        let outcome = classify(&response(200, Vec::new(), "<xml>nope</xml>"));
        assert!(matches!(outcome, ResponseOutcome::MalformedBody { .. }));
    }

    /// No Instagram publishing call answers with an empty success body.
    ///
    /// This is the counterpart of the X `APPEND` 204 bug class: there, a
    /// bodyless success was real and the handler had to tolerate it; here every
    /// documented success carries JSON (`{"id": …}` from container create and
    /// publish, `{"status_code": …}` from polling). So an empty 2xx is *not* a
    /// success, and reporting one would publish nothing while telling the caller
    /// it had. The empty body is the bodyless case this provider has to refuse.
    #[test]
    fn an_empty_2xx_body_is_not_a_facebook_style_success() {
        for status in [200_u16, 201, 204] {
            let outcome = classify(&response(status, Vec::new(), ""));
            assert!(
                matches!(outcome, ResponseOutcome::MalformedBody { .. }),
                "HTTP {status} with no body must not classify as success"
            );
        }
    }

    #[test]
    fn retry_after_can_come_from_the_body() {
        assert_eq!(parse_retry_after_text("retry-after: 15"), Some(15));
        assert_eq!(parse_retry_after_text("no hint here"), None);
    }
}
