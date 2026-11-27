//! The dashboard HTTP surface, on axum, built from a route registry.
//!
//! Scope, stated plainly: this crate serves the routes the Rust engine can
//! answer **today** and refuses the rest with a typed `501` that names the
//! reason. There is no route here that returns a plausible-looking `200` for
//! work the engine does not do.
//!
//! ```text
//! request ──▶ auth rule (auth) ──▶ registry (registry) ──▶ handler (handlers)
//!                                      │
//!                                      ├─ not implemented ──▶ 501 + reason
//!                                      └─ unknown path ────▶ static shell / 404
//! ```
//!
//! # The registry is the router
//!
//! [`registry::ROUTES`] is the single table of `(method, path)`. Adding a route
//! means adding a row; removing one means deleting a row. The same table is
//! what the parity test in `tests/route_parity.rs` compares against the Python
//! FastAPI app, so a route cannot exist in one surface and not the other
//! without a test failing. `axum` supplies the HTTP plumbing (request and
//! response types, the body limit, the server), and a single table-driven
//! fallback performs the match — deliberately: one row per route is what makes
//! "everything the engine cannot do is a 501" a checkable property.
//!
//! # The analytics-backed reads
//!
//! `/api/summary`, `/state`, `/api/analytics/outcomes`, `/api/videos`,
//! `/api/videos/{video_id}` and `/api/activity` are served from `state.json`
//! plus `analytics.db` — no Python import, no socket. The documents are built
//! by `xpst-surface`'s dashboard and analytics builders, the same code the CLI
//! and MCP surfaces render, and `tests/analytics_parity.rs` compares the bodies
//! byte for byte against a capture from the real Python app over a seeded
//! fixture (a fictional `state.json` and an `analytics.db` created by Python's
//! `AnalyticsStore`, clock pinned). Re-capture with
//! `cargo xtask analytics-routes --write`.
//!
//! # Public by design, and nothing else
//!
//! [`auth::PUBLIC_BY_DESIGN`] enumerates every path that answers without a
//! credential, each with its reason (a health probe cannot carry a login, an
//! OAuth redirect cannot attach a header, a brand-new phone has no token yet,
//! and the static shell must load or the pairing screen is unreachable).
//! Everything else that mutates requires a credential under every bind, and
//! `/api` reads require one on a non-loopback bind. The rule is ported from
//! `src/xpst/dashboard/auth.py`; the enumeration is asserted against the
//! registry so documentation and enforcement cannot drift.
//!
//! # Deliberate divergences from the Python surface
//!
//! Named, not hidden (the port policy is "deviate only where the deviation is
//! stated"). [`parity::DIVERGENCES`] carries the same list as data.
//!
//! | Surface | Python | Here | Why |
//! |---|---|---|---|
//! | live auth probes | `collect_live_auth_status` per platform | none; `readiness.pending` is `true` | the Rust engine has no live prober yet, and a config-only "ready" is a lie Python already refused to tell |
//! | `/api/providers` state fields | live/credential probe results | `null` / `unconfigured` | nothing is claimed that was not checked |
//! | provider display names | product copy (`YouTube Shorts`) | `xpst-core`'s profile name (`YouTube`) | the content contract crate owns that spelling |
//! | rate-limit ledger files | `rate_cooldowns.json` | `rate_limits.json` (`xpst-limits`) | the Rust ledger is the P2 slice's; the two do not share a schema yet |
//! | schedule rows | adds `scheduled_time_local` | persisted entry verbatim | no timezone database in the engine yet; the store is UTC |
//! | plan records | `plan_service` preview + digest | pipeline-native record, same top-level keys | the Rust engine freezes plans in `xpst-pipeline` |
//! | `/metrics` | Prometheus text | typed `501` | no metrics registry in the Rust engine |
//! | `/api/analytics/outcomes?live=1` | a real collection against every platform API | typed `501` (`LIVE_COLLECTION_NOT_IMPLEMENTED`) | the collection is the network tranche; `live=0` (the default) serves the recorded report |
//! | YouTube ownership in the outcome report | `channels.list` + a paginated `playlistItems` walk | `unverifiable`, so the platform fails closed | the same verdict Python reaches when the check cannot run |
//! | `/api/activity` | `StateStore` creates the config dir, `backups/` and `.state.lock` on construction | reads `state.json` only | a GET should not create files |
//! | a `state.json` whose root is not an object | `AttributeError` → `500` | the empty document | a corrupt file must not 500 the page |
//! | unmatched `/api/*` on a non-loopback bind | `401` | `401` | same rule, kept: the gate is by path, not by table row |

#![forbid(unsafe_code)]

pub mod app;
pub mod auth;
pub mod bind;
pub mod devices;
pub mod handlers;
pub mod parity;
pub mod registry;

pub use app::{build_router, dispatch, AppState, PublisherRegistry};
pub use auth::{AuthConfig, BasicLogin, Credential, PublicReason, PUBLIC_BY_DESIGN};
pub use bind::{is_loopback, BindError, BindPlan, LOOPBACK_HOSTS};
pub use registry::{lookup, Access, Handler, Method, RouteSpec, RouteStatus, ROUTES};
