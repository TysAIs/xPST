//! SSRF guard: `net_guard.validate_public_url` ported from Python.
//!
//! `src/xpst/utils/net_guard.py` is the single choke point for every URL xPST
//! fetches from state, config or an agent argument. On a local-first desktop app
//! the machine itself is the crown jewel: a loopback fetch reaches the dashboard
//! (`/state`, `/api/*`), and `169.254.169.254` reaches cloud instance metadata
//! from any VM the user runs xPST on. The MCP server runs this over every tool's
//! URL-shaped arguments before dispatch, so an agent cannot hand the Rust server
//! a URL the shipped Python server refuses.
//!
//! The port is held to `tests/fixtures/python_net_guard.json`, captured from the
//! real function by `xtask/scripts/capture_net_guard.py` — 235 rows covering
//! every scheme, the literal IPv4/IPv6 blocklist, resolving and non-resolving
//! hostnames, credentials, ports, the three escape hatches and `resolve=False`.
//! The blocklist tables are asserted against the same capture, so a Python
//! upgrade that moves a range fails the Rust test instead of silently widening
//! the guard.
//!
//! ## The resolver decision
//!
//! Python calls `socket.getaddrinfo`, which blocks with no timeout. The obvious
//! Rust equivalent, `std::net::ToSocketAddrs`, has the same defect: a hostile
//! hostname whose authoritative server never answers parks the calling thread
//! for as long as the system resolver's own timeout allows (tens of seconds,
//! and `resolv.conf` can raise it). On a stdio MCP server that is a
//! denial-of-service an agent can trigger with one argument.
//!
//! So [`SystemResolver`] runs `to_socket_addrs` on a worker thread and waits
//! [`SystemResolver::timeout`] for the answer. A timeout is a resolution
//! failure and answers the same sentence Python answers for NXDOMAIN
//! (`Could not resolve host: …`) — fail-closed, and the wire document is
//! unchanged. The worker thread is detached and cannot be cancelled, so
//! in-flight resolutions are capped at [`SystemResolver::max_in_flight`]; past
//! the cap every further lookup fails closed immediately instead of growing the
//! thread pool without bound.
//!
//! ## Recorded divergences
//!
//! * **Timeout.** Python waits for the system resolver indefinitely; this waits
//!   [`DEFAULT_DNS_TIMEOUT`] and then refuses. Same verdict for every host that
//!   does not answer, different latency.
//! * **IDNA.** Python's `getaddrinfo` IDNA-encodes a non-ASCII hostname before
//!   the lookup; `to_socket_addrs` passes the UTF-8 bytes to `getaddrinfo`,
//!   which does not. Both fail to resolve a name that only exists in its
//!   punycode form, so the verdict matches; the resolver sees a different string.
//! * **`str.strip()` vs `trim()`.** Python's `str.strip()` also strips the
//!   `\x1c`–`\x1f` separators, which `char::is_whitespace` does not. They are
//!   stripped explicitly here so the returned value matches.
//! * **NFKC.** `_checknetloc` normalises the whole netloc; this checks the
//!   nineteen codepoints whose NFKC expansion contains a delimiter (captured,
//!   not retyped). A codepoint outside that table cannot introduce a delimiter,
//!   so the accept/reject set is identical.
//! * **Deprecated site-local IPv6 (`fec0::/10`) is ALLOWED**, because Python
//!   allows it: it is in neither `_private_networks` nor `_reserved_networks`.
//!   Reproduced rather than fixed — the wire contract is the shipped one — and
//!   pinned by the `ipv6-fec0::1` row so a future Python fix is visible.

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, ToSocketAddrs};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc;
use std::time::Duration;

/// `DEFAULT_ALLOWED_SCHEMES`.
pub const DEFAULT_ALLOWED_SCHEMES: [&str; 2] = ["http", "https"];

/// How long [`SystemResolver`] waits for the system resolver before refusing.
pub const DEFAULT_DNS_TIMEOUT: Duration = Duration::from_secs(5);

/// Cloud metadata endpoints that are link-local or otherwise special-cased
/// (`_METADATA_ADDRESSES`).
const METADATA_ADDRESSES: [&str; 5] = [
    "169.254.169.254", // AWS / GCP / Azure IMDS
    "169.254.169.253",
    "100.100.100.200", // Alibaba Cloud
    "fd00:ec2::254",   // AWS IPv6 IMDS
    "192.0.0.192",     // Oracle Cloud
];

/// Hostnames that always mean "this machine" (or a cloud metadata service) and
/// are refused without a DNS round-trip (`_BLOCKED_HOSTNAMES`).
const BLOCKED_HOSTNAMES: [&str; 8] = [
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata",
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
];

/// Carrier-grade NAT (RFC 6598) — `is_private` is false for this range.
const CGNAT: &str = "100.64.0.0/10";

/// `ipaddress._IPv4Constants._private_networks`.
const IPV4_PRIVATE: [&str; 14] = [
    "0.0.0.0/8",
    "10.0.0.0/8",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.0.170/31",
    "192.0.2.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "240.0.0.0/4",
    "255.255.255.255/32",
];

/// `ipaddress._IPv4Constants._private_networks_exceptions` — the two addresses
/// inside `192.0.0.0/24` that are globally reachable.
const IPV4_PRIVATE_EXCEPTIONS: [&str; 2] = ["192.0.0.9/32", "192.0.0.10/32"];

/// `ipaddress._IPv4Constants._reserved_network`.
const IPV4_RESERVED: [&str; 1] = ["240.0.0.0/4"];

/// `ipaddress._IPv4Constants._multicast_network`.
const IPV4_MULTICAST: [&str; 1] = ["224.0.0.0/4"];

/// `ipaddress._IPv4Constants._loopback_network`.
const IPV4_LOOPBACK: [&str; 1] = ["127.0.0.0/8"];

/// `ipaddress._IPv4Constants._linklocal_network`.
const IPV4_LINK_LOCAL: [&str; 1] = ["169.254.0.0/16"];

/// `ipaddress._IPv6Constants._private_networks`.
const IPV6_PRIVATE: [&str; 11] = [
    "::1/128",
    "::/128",
    "::ffff:0.0.0.0/96",
    "64:ff9b:1::/48",
    "100::/64",
    "2001::/23",
    "2001:db8::/32",
    "2002::/16",
    "3fff::/20",
    "fc00::/7",
    "fe80::/10",
];

/// `ipaddress._IPv6Constants._private_networks_exceptions`.
const IPV6_PRIVATE_EXCEPTIONS: [&str; 6] = [
    "2001:1::1/128",
    "2001:1::2/128",
    "2001:3::/32",
    "2001:4:112::/48",
    "2001:20::/28",
    "2001:30::/28",
];

/// `ipaddress._IPv6Constants._reserved_networks`.
const IPV6_RESERVED: [&str; 15] = [
    "::/8", "100::/8", "200::/7", "400::/6", "800::/5", "1000::/4", "4000::/3", "6000::/3",
    "8000::/3", "a000::/3", "c000::/3", "e000::/4", "f000::/5", "f800::/6", "fe00::/9",
];

/// `ipaddress._IPv6Constants._multicast_network`.
const IPV6_MULTICAST: [&str; 1] = ["ff00::/8"];

/// `ipaddress._IPv6Constants._linklocal_network`.
const IPV6_LINK_LOCAL: [&str; 1] = ["fe80::/10"];

/// The codepoints whose NFKC expansion contains `/`, `?`, `#`, `@` or `:`.
///
/// Captured by `capture_net_guard.py` scanning U+0080–U+2FFFF, and asserted
/// against the fixture. `_checknetloc` normalises the whole netloc and refuses
/// when a delimiter appears; only these codepoints can introduce one.
const NFKC_DELIMITERS: [(char, &str); 19] = [
    ('\u{2047}', "??"),
    ('\u{2048}', "?!"),
    ('\u{2049}', "!?"),
    ('\u{2100}', "a/c"),
    ('\u{2101}', "a/s"),
    ('\u{2105}', "c/o"),
    ('\u{2106}', "c/u"),
    ('\u{2a74}', "::="),
    ('\u{fe13}', ":"),
    ('\u{fe16}', "?"),
    ('\u{fe55}', ":"),
    ('\u{fe56}', "?"),
    ('\u{fe5f}', "#"),
    ('\u{fe6b}', "@"),
    ('\u{ff03}', "#"),
    ('\u{ff0f}', "/"),
    ('\u{ff1a}', ":"),
    ('\u{ff1f}', "?"),
    ('\u{ff20}', "@"),
];

/// A URL the guard refused. `reason` is the `BlockedURLError` sentence, which
/// never echoes a credential from the URL.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BlockedUrlError {
    pub reason: String,
}

impl BlockedUrlError {
    fn new(reason: impl Into<String>) -> Self {
        Self {
            reason: reason.into(),
        }
    }
}

impl std::fmt::Display for BlockedUrlError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.reason)
    }
}

impl std::error::Error for BlockedUrlError {}

/// The keyword arguments of `validate_public_url`, with Python's defaults.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UrlPolicy {
    /// Permitted URL schemes, compared against the lowercased scheme.
    pub allowed_schemes: Vec<String>,
    /// Exact hostnames that bypass the address checks.
    pub allow_hosts: Vec<String>,
    /// Skip the address blocklist entirely.
    pub allow_private: bool,
    /// When set, only these ports are permitted.
    pub allow_ports: Option<Vec<u16>>,
    /// When false, do not resolve DNS (literal-IP and scheme checks still apply).
    pub resolve: bool,
}

impl Default for UrlPolicy {
    fn default() -> Self {
        Self {
            allowed_schemes: DEFAULT_ALLOWED_SCHEMES
                .iter()
                .map(|scheme| (*scheme).to_string())
                .collect(),
            allow_hosts: Vec::new(),
            allow_private: false,
            allow_ports: None,
            resolve: true,
        }
    }
}

/// Why a hostname could not be turned into addresses.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ResolveFailure {
    /// The resolver answered "no such host" (Python's `gaierror`).
    NotFound,
    /// The resolver did not answer inside the bound.
    TimedOut,
}

/// The DNS boundary, injected so a test can pin every resolving row.
pub trait HostResolver {
    fn resolve(&self, host: &str) -> Result<Vec<IpAddr>, ResolveFailure>;
}

/// The system resolver, bounded by a timeout and an in-flight cap.
#[derive(Debug, Clone)]
pub struct SystemResolver {
    pub timeout: Duration,
    pub max_in_flight: usize,
}

impl Default for SystemResolver {
    fn default() -> Self {
        Self {
            timeout: DEFAULT_DNS_TIMEOUT,
            max_in_flight: 8,
        }
    }
}

/// Resolutions currently occupying a worker thread. A timed-out lookup keeps
/// its slot until the thread returns, which is what bounds the thread count.
static IN_FLIGHT: AtomicUsize = AtomicUsize::new(0);

impl HostResolver for SystemResolver {
    fn resolve(&self, host: &str) -> Result<Vec<IpAddr>, ResolveFailure> {
        if IN_FLIGHT.fetch_add(1, Ordering::SeqCst) >= self.max_in_flight {
            IN_FLIGHT.fetch_sub(1, Ordering::SeqCst);
            return Err(ResolveFailure::TimedOut);
        }
        let (sender, receiver) = mpsc::channel();
        let owned = host.to_string();
        std::thread::spawn(move || {
            let answer = (owned.as_str(), 0u16)
                .to_socket_addrs()
                .map(|addresses| addresses.map(|address| address.ip()).collect::<Vec<_>>())
                .map_err(|_| ResolveFailure::NotFound);
            let _ = sender.send(answer);
            IN_FLIGHT.fetch_sub(1, Ordering::SeqCst);
        });
        match receiver.recv_timeout(self.timeout) {
            Ok(answer) => answer,
            Err(_) => Err(ResolveFailure::TimedOut),
        }
    }
}

/// `validate_public_url()`: the URL unchanged, or the refusal.
///
/// `policy` carries Python's keyword arguments and `resolver` is the DNS
/// boundary. Use [`validate_public_url_default`] for the shipped defaults.
pub fn validate_public_url(
    url: &str,
    policy: &UrlPolicy,
    resolver: &dyn HostResolver,
) -> Result<String, BlockedUrlError> {
    let candidate = python_strip(url);
    if candidate.is_empty() {
        return Err(BlockedUrlError::new("URL is empty"));
    }

    let parsed = split_url(&candidate).map_err(|_| BlockedUrlError::new("Malformed URL"))?;

    let scheme = parsed.scheme.to_ascii_lowercase();
    if !policy
        .allowed_schemes
        .iter()
        .any(|allowed| allowed.as_str() == scheme)
    {
        let shown = if scheme.is_empty() {
            "(none)"
        } else {
            scheme.as_str()
        };
        return Err(BlockedUrlError::new(format!("Scheme not allowed: {shown}")));
    }

    let hostname = match parsed.hostname.as_deref() {
        Some(hostname) if !hostname.is_empty() => hostname,
        _ => return Err(BlockedUrlError::new("URL has no host")),
    };

    // Python tests `parsed.username or parsed.password`, so an EMPTY userinfo
    // (`http://@host/`, `http://:@host/`) is falsy and is not a credential.
    let has_credentials = parsed
        .username
        .as_deref()
        .is_some_and(|username| !username.is_empty())
        || parsed
            .password
            .as_deref()
            .is_some_and(|password| !password.is_empty());
    if has_credentials {
        return Err(BlockedUrlError::new("URL must not embed credentials"));
    }

    if let (Some(allowed), Some(port)) = (policy.allow_ports.as_ref(), parsed.port) {
        if !allowed.contains(&port) {
            return Err(BlockedUrlError::new(format!("Port not allowed: {port}")));
        }
    }

    if policy.allow_private
        || policy
            .allow_hosts
            .iter()
            .any(|allowed| allowed.to_lowercase() == hostname)
    {
        return Ok(candidate);
    }

    // Name-based blocklist first: independent of the address checks and of the
    // `resolve` flag, so "localhost" can never slip through `resolve=false`.
    let lowered = hostname.to_lowercase();
    let lowered = lowered.trim_end_matches('.');
    if BLOCKED_HOSTNAMES.contains(&lowered) || lowered.ends_with(".localhost") {
        return Err(BlockedUrlError::new(format!(
            "Host is not a public destination: {hostname}"
        )));
    }

    let addresses: Vec<IpAddr> = match parse_ip_literal(hostname) {
        // A bare IP literal is checked directly; only real names need DNS.
        Some(address) => vec![address],
        None if policy.resolve => resolver.resolve(hostname).map_err(|failure| {
            // A timeout is a resolution failure: fail closed with Python's
            // NXDOMAIN sentence rather than inventing a second wire document.
            let _ = failure;
            BlockedUrlError::new(format!("Could not resolve host: {hostname}"))
        })?,
        None => Vec::new(),
    };

    for address in addresses {
        if is_blocked_ip(address) {
            return Err(BlockedUrlError::new(format!(
                "Host resolves to a non-public address and was refused: {hostname}"
            )));
        }
    }

    Ok(candidate)
}

/// [`validate_public_url`] with the shipped policy and the system resolver.
pub fn validate_public_url_default(url: &str) -> Result<String, BlockedUrlError> {
    validate_public_url(url, &UrlPolicy::default(), &SystemResolver::default())
}

/// `is_blocked_ip()`: whether an address points somewhere xPST must never fetch.
///
/// IPv4-mapped and 6to4 IPv6 addresses are unwrapped first, because
/// `IPv6Address.is_loopback` is false for `::ffff:127.0.0.1`.
pub fn is_blocked_ip(address: IpAddr) -> bool {
    let address = match address {
        IpAddr::V6(v6) => match v6.to_ipv4_mapped() {
            Some(mapped) => IpAddr::V4(mapped),
            None => match six_to_four(v6) {
                Some(embedded) => IpAddr::V4(embedded),
                None => IpAddr::V6(v6),
            },
        },
        other => other,
    };

    if METADATA_ADDRESSES.contains(&address.to_string().as_str()) {
        return true;
    }

    match address {
        IpAddr::V4(v4) => {
            in_any_v4(&IPV4_LOOPBACK, v4)
                || in_any_v4(&IPV4_LINK_LOCAL, v4)
                || (in_any_v4(&IPV4_PRIVATE, v4) && !in_any_v4(&IPV4_PRIVATE_EXCEPTIONS, v4))
                || in_any_v4(&IPV4_RESERVED, v4)
                || in_any_v4(&IPV4_MULTICAST, v4)
                || v4.is_unspecified()
                || in_any_v4(&[CGNAT], v4)
        }
        IpAddr::V6(v6) => {
            v6.is_loopback()
                || in_any_v6(&IPV6_LINK_LOCAL, v6)
                || (in_any_v6(&IPV6_PRIVATE, v6) && !in_any_v6(&IPV6_PRIVATE_EXCEPTIONS, v6))
                || in_any_v6(&IPV6_RESERVED, v6)
                || in_any_v6(&IPV6_MULTICAST, v6)
                || v6.is_unspecified()
        }
    }
}

/// `IPv6Address.sixtofour`: the IPv4 address embedded in `2002::/16`.
fn six_to_four(address: Ipv6Addr) -> Option<Ipv4Addr> {
    let segments = address.segments();
    if segments[0] != 0x2002 {
        return None;
    }
    Some(Ipv4Addr::new(
        (segments[1] >> 8) as u8,
        segments[1] as u8,
        (segments[2] >> 8) as u8,
        segments[2] as u8,
    ))
}

/// `ipaddress.ip_address()` for a hostname, including an IPv6 zone id.
///
/// Python accepts a scope id on an IPv6 literal (`fe80::1%eth0`) and rejects one
/// on IPv4, so the scope is only stripped for the IPv6 attempt.
fn parse_ip_literal(value: &str) -> Option<IpAddr> {
    if let Ok(v4) = value.parse::<Ipv4Addr>() {
        return Some(IpAddr::V4(v4));
    }
    if let Ok(v6) = value.parse::<Ipv6Addr>() {
        return Some(IpAddr::V6(v6));
    }
    let (address, _scope) = value.split_once('%')?;
    address.parse::<Ipv6Addr>().ok().map(IpAddr::V6)
}

fn in_any_v4(networks: &[&str], address: Ipv4Addr) -> bool {
    networks
        .iter()
        .any(|network| cidr4_matches(network, address))
}

fn in_any_v6(networks: &[&str], address: Ipv6Addr) -> bool {
    networks
        .iter()
        .any(|network| cidr6_matches(network, address))
}

fn cidr4_matches(network: &str, address: Ipv4Addr) -> bool {
    let (base, prefix) = network.split_once('/').expect("a CIDR has a prefix");
    let base: Ipv4Addr = base.parse().expect("a CIDR base is an IPv4 address");
    let prefix: u32 = prefix.parse().expect("a CIDR prefix is a number");
    let mask = if prefix == 0 {
        0
    } else {
        u32::MAX << (32 - prefix)
    };
    (u32::from(base) & mask) == (u32::from(address) & mask)
}

fn cidr6_matches(network: &str, address: Ipv6Addr) -> bool {
    let (base, prefix) = network.split_once('/').expect("a CIDR has a prefix");
    let base: Ipv6Addr = base.parse().expect("a CIDR base is an IPv6 address");
    let prefix: u32 = prefix.parse().expect("a CIDR prefix is a number");
    let mask = if prefix == 0 {
        0
    } else {
        u128::MAX << (128 - prefix)
    };
    (u128::from(base) & mask) == (u128::from(address) & mask)
}

/// `str.strip()`: whitespace plus the `\x1c`–`\x1f` separators Python counts.
fn python_strip(value: &str) -> String {
    value
        .trim_matches(|character: char| {
            character.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&character)
        })
        .to_string()
}

/// The pieces of a URL the guard reads, after `urlsplit`'s normalisation.
struct SplitUrl {
    scheme: String,
    hostname: Option<String>,
    port: Option<u16>,
    username: Option<String>,
    password: Option<String>,
}

/// `urllib.parse.urlsplit()` — the parts the guard reads, or `Err` for the
/// `ValueError`s it raises ("Malformed URL").
///
/// Reproduced rather than approximated because the differences are the attack
/// surface: `\t`, `\r` and `\n` are removed before parsing, a leading run of C0
/// controls and spaces is stripped, the hostname is lowercased except for its
/// zone id, and a bracketed host must be an IPv6 address or an IPvFuture.
fn split_url(url: &str) -> Result<SplitUrl, ()> {
    // `url.lstrip(_WHATWG_C0_CONTROL_OR_SPACE)` then `_UNSAFE_URL_BYTES_TO_REMOVE`.
    let mut rest: String = url
        .trim_start_matches(|character: char| character <= ' ')
        .chars()
        .filter(|character| !matches!(character, '\t' | '\r' | '\n'))
        .collect();

    let mut scheme = String::new();
    if let Some(index) = rest.find(':') {
        let head = &rest[..index];
        let starts_alpha = rest
            .chars()
            .next()
            .is_some_and(|first| first.is_ascii_alphabetic());
        if index > 0 && starts_alpha && head.chars().all(is_scheme_char) {
            scheme = head.to_ascii_lowercase();
            rest = rest[index + 1..].to_string();
        }
    }

    let mut netloc = String::new();
    if let Some(after) = rest.strip_prefix("//") {
        let delim = after
            .char_indices()
            .find(|(_, character)| matches!(character, '/' | '?' | '#'))
            .map(|(index, _)| index)
            .unwrap_or(after.len());
        netloc = after[..delim].to_string();
    }

    let has_open = netloc.contains('[');
    let has_close = netloc.contains(']');
    if has_open != has_close {
        return Err(());
    }
    if has_open && has_close {
        check_bracketed_netloc(&netloc)?;
    }
    check_netloc(&netloc)?;

    let hostinfo = match netloc.rsplit_once('@') {
        Some((_, hostinfo)) => hostinfo,
        None => netloc.as_str(),
    };

    let (raw_hostname, raw_port) = match hostinfo.split_once('[') {
        Some((_, bracketed)) => match bracketed.split_once(']') {
            Some((hostname, after)) => (
                hostname,
                after.split_once(':').map(|(_, port)| port).unwrap_or(""),
            ),
            None => (bracketed, ""),
        },
        None => match hostinfo.split_once(':') {
            Some((hostname, port)) => (hostname, port),
            None => (hostinfo, ""),
        },
    };

    // `hostname`: the part before a zone id is lowercased, the zone is not.
    let hostname = if raw_hostname.is_empty() {
        None
    } else {
        Some(match raw_hostname.split_once('%') {
            Some((address, zone)) => format!("{}%{zone}", address.to_lowercase()),
            None => raw_hostname.to_lowercase(),
        })
    };

    let port = if raw_port.is_empty() {
        None
    } else {
        if !raw_port.chars().all(|character| character.is_ascii_digit()) {
            return Err(());
        }
        let value: u64 = raw_port.parse().map_err(|_| ())?;
        if value > u64::from(u16::MAX) {
            return Err(());
        }
        Some(value as u16)
    };

    let (username, password) = match netloc.rsplit_once('@') {
        Some((userinfo, _)) => match userinfo.split_once(':') {
            Some((username, password)) => (Some(username.to_string()), Some(password.to_string())),
            None => (Some(userinfo.to_string()), None),
        },
        None => (None, None),
    };

    Ok(SplitUrl {
        scheme,
        hostname,
        port,
        username,
        password,
    })
}

fn is_scheme_char(character: char) -> bool {
    character.is_ascii_alphanumeric() || matches!(character, '+' | '-' | '.')
}

/// `_check_bracketed_netloc()`.
fn check_bracketed_netloc(netloc: &str) -> Result<(), ()> {
    let hostname_and_port = match netloc.rsplit_once('@') {
        Some((_, hostinfo)) => hostinfo,
        None => netloc,
    };
    let (hostname, port) = match hostname_and_port.split_once('[') {
        Some((before, bracketed)) => {
            // No data is allowed before a bracket.
            if !before.is_empty() {
                return Err(());
            }
            let (hostname, after) = bracketed.split_once(']').unwrap_or((bracketed, ""));
            // No data is allowed after the bracket but before the port delimiter.
            if !after.is_empty() && !after.starts_with(':') {
                return Err(());
            }
            (hostname, after)
        }
        None => match hostname_and_port.split_once(':') {
            Some((hostname, port)) => (hostname, port),
            None => (hostname_and_port, ""),
        },
    };
    let _ = port;
    check_bracketed_host(hostname)
}

/// `_check_bracketed_host()`: an IPvFuture literal or an IPv6 address.
fn check_bracketed_host(hostname: &str) -> Result<(), ()> {
    if hostname.starts_with(['v', 'V']) {
        // `\A[vV][a-fA-F0-9]+\..+\Z`
        let mut characters = hostname.chars();
        characters.next();
        let digits: String = characters
            .clone()
            .take_while(|character| character.is_ascii_hexdigit())
            .collect();
        if digits.is_empty() {
            return Err(());
        }
        let remainder: String = characters.skip(digits.len()).collect();
        let valid = remainder
            .strip_prefix('.')
            .is_some_and(|tail| !tail.is_empty());
        return if valid { Ok(()) } else { Err(()) };
    }
    match parse_ip_literal(hostname) {
        Some(IpAddr::V6(_)) => Ok(()),
        // An IPv4 address cannot be in brackets, and a non-address is invalid.
        _ => Err(()),
    }
}

/// `_checknetloc()`: a non-ASCII netloc whose NFKC expansion introduces a
/// delimiter is refused.
fn check_netloc(netloc: &str) -> Result<(), ()> {
    if netloc.is_ascii() {
        return Ok(());
    }
    // Python removes the delimiters that are already there before normalising,
    // so only an expansion can introduce one.
    let stripped: String = netloc
        .chars()
        .filter(|character| !matches!(character, '@' | ':' | '#' | '?'))
        .collect();
    for character in stripped.chars() {
        if let Some((_, expansion)) = NFKC_DELIMITERS
            .iter()
            .find(|(candidate, _)| *candidate == character)
        {
            if expansion
                .chars()
                .any(|introduced| matches!(introduced, '/' | '?' | '#' | '@' | ':'))
            {
                return Err(());
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Stub(Vec<IpAddr>);

    impl HostResolver for Stub {
        fn resolve(&self, _host: &str) -> Result<Vec<IpAddr>, ResolveFailure> {
            Ok(self.0.clone())
        }
    }

    fn blocked(url: &str) -> String {
        validate_public_url(url, &UrlPolicy::default(), &Stub(Vec::new()))
            .expect_err("refused")
            .reason
    }

    #[test]
    fn a_public_literal_is_accepted_and_returned_unchanged() {
        assert_eq!(
            validate_public_url_default("http://1.1.1.1/x").expect("accepted"),
            "http://1.1.1.1/x"
        );
    }

    #[test]
    fn a_loopback_literal_is_refused() {
        assert_eq!(
            blocked("http://127.0.0.1/x"),
            "Host resolves to a non-public address and was refused: 127.0.0.1"
        );
    }

    #[test]
    fn the_metadata_endpoint_is_refused() {
        assert_eq!(
            blocked("http://169.254.169.254/latest/meta-data/"),
            "Host resolves to a non-public address and was refused: 169.254.169.254"
        );
    }

    #[test]
    fn a_blocked_hostname_is_refused_without_resolving() {
        assert_eq!(
            blocked("http://localhost/x"),
            "Host is not a public destination: localhost"
        );
    }

    #[test]
    fn an_ipv4_mapped_loopback_is_unwrapped() {
        assert_eq!(
            blocked("http://[::ffff:127.0.0.1]/x"),
            "Host resolves to a non-public address and was refused: ::ffff:127.0.0.1"
        );
    }

    #[test]
    fn a_six_to_four_loopback_is_unwrapped() {
        assert_eq!(
            blocked("http://[2002:7f00:1::]/x"),
            "Host resolves to a non-public address and was refused: 2002:7f00:1::"
        );
    }

    #[test]
    fn credentials_are_refused_before_the_address_checks() {
        assert_eq!(
            blocked("http://user:pass@127.0.0.1/x"),
            "URL must not embed credentials"
        );
    }

    #[test]
    fn a_bad_scheme_is_refused() {
        assert_eq!(blocked("ftp://1.1.1.1/x"), "Scheme not allowed: ftp");
    }

    #[test]
    fn a_malformed_port_is_a_malformed_url() {
        assert_eq!(blocked("http://1.1.1.1:99999/x"), "Malformed URL");
    }

    #[test]
    fn allow_private_returns_before_the_name_blocklist() {
        let policy = UrlPolicy {
            allow_private: true,
            ..UrlPolicy::default()
        };
        assert_eq!(
            validate_public_url("http://localhost/x", &policy, &Stub(Vec::new())).expect("allowed"),
            "http://localhost/x"
        );
    }

    #[test]
    fn a_resolver_timeout_fails_closed_with_the_nxdomain_sentence() {
        struct TimingOut;
        impl HostResolver for TimingOut {
            fn resolve(&self, _host: &str) -> Result<Vec<IpAddr>, ResolveFailure> {
                Err(ResolveFailure::TimedOut)
            }
        }
        let error = validate_public_url("http://slow.test/x", &UrlPolicy::default(), &TimingOut)
            .expect_err("refused");
        assert_eq!(error.reason, "Could not resolve host: slow.test");
    }

    #[test]
    fn a_name_that_resolves_to_a_private_address_is_refused() {
        let stub = Stub(vec!["10.0.0.5".parse().expect("address")]); // public-safety: allow-private-ip
        let error = validate_public_url("http://private.test/x", &UrlPolicy::default(), &stub)
            .expect_err("refused");
        assert_eq!(
            error.reason,
            "Host resolves to a non-public address and was refused: private.test"
        );
    }

    #[test]
    fn resolve_false_skips_dns_but_not_the_literal_check() {
        let policy = UrlPolicy {
            resolve: false,
            ..UrlPolicy::default()
        };
        assert!(validate_public_url("http://example.test/x", &policy, &Stub(Vec::new())).is_ok());
        assert!(validate_public_url("http://127.0.0.1/x", &policy, &Stub(Vec::new())).is_err());
    }

    #[test]
    fn a_zone_id_is_kept_but_the_address_is_still_checked() {
        assert_eq!(
            blocked("http://[fe80::1%eth0]/x"),
            "Host resolves to a non-public address and was refused: fe80::1%eth0"
        );
    }

    #[test]
    fn the_tables_match_the_captured_python_constants() {
        // The fixture is the authority; this only guards the transcription.
        let fixture: serde_json::Value =
            serde_json::from_str(include_str!("../tests/fixtures/python_net_guard.json"))
                .expect("fixture");
        let constants = &fixture["constants"];
        let strings = |value: &serde_json::Value| -> Vec<String> {
            value
                .as_array()
                .expect("array")
                .iter()
                .map(|item| item.as_str().expect("string").to_string())
                .collect()
        };
        assert_eq!(
            strings(&constants["ipv4"]["private"]),
            IPV4_PRIVATE.map(str::to_string).to_vec()
        );
        assert_eq!(
            strings(&constants["ipv4"]["private_exceptions"]),
            IPV4_PRIVATE_EXCEPTIONS.map(str::to_string).to_vec()
        );
        assert_eq!(
            strings(&constants["ipv6"]["private"]),
            IPV6_PRIVATE.map(str::to_string).to_vec()
        );
        assert_eq!(
            strings(&constants["ipv6"]["private_exceptions"]),
            IPV6_PRIVATE_EXCEPTIONS.map(str::to_string).to_vec()
        );
        assert_eq!(
            strings(&constants["ipv6"]["reserved"]),
            IPV6_RESERVED.map(str::to_string).to_vec()
        );
        // Membership tables: the capture sorts them, so compare as sets.
        let sorted = |mut values: Vec<String>| {
            values.sort();
            values
        };
        assert_eq!(
            sorted(strings(&constants["metadata_addresses"])),
            sorted(METADATA_ADDRESSES.map(str::to_string).to_vec())
        );
        assert_eq!(
            sorted(strings(&constants["blocked_hostnames"])),
            sorted(BLOCKED_HOSTNAMES.map(str::to_string).to_vec())
        );
        let nfkc: Vec<(u32, String)> = constants["nfkc_delimiters"]
            .as_array()
            .expect("array")
            .iter()
            .map(|entry| {
                (
                    entry["codepoint"].as_u64().expect("codepoint") as u32,
                    entry["expansion"].as_str().expect("expansion").to_string(),
                )
            })
            .collect();
        let ours: Vec<(u32, String)> = NFKC_DELIMITERS
            .iter()
            .map(|(character, expansion)| (*character as u32, (*expansion).to_string()))
            .collect();
        assert_eq!(nfkc, ours);
    }
}
