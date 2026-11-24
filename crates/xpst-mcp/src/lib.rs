//! rmcp tool surface, same tool names and schemas (P4).
//!
//! The product rule this crate implements: *every capability the UI has, the
//! agent has.* The Python engine serves 46 MCP tools; the Rust surface must
//! eventually serve the same names with the same semantics, and it may never
//! claim a tool it does not implement.
//!
//! That rule is enforced structurally, not by review:
//!
//! * [`registry`] embeds the tool definitions captured from the Python
//!   registry (`tests/fixtures/python_tool_registry.json`), so names and input
//!   schemas cannot be hand-written into drift.
//! * Every row states its [`registry::Implementation`]: a ported handler, or a
//!   stub with the reason it is not ported. A stub can only answer with a
//!   typed `not_implemented` error — there is no path from a stub to a
//!   plausible-looking success.
//! * [`guard`] carries the consent tier (read-only vs mutating) and refuses
//!   mutating calls unless the operator opted in, matching the Python server's
//!   `_guardrail_block`.
//! * `tests/parity.rs` fails on drift against `src/xpst/mcp/server.py`.
//!
//! This slice ports the read-only tools that need no network and no platform
//! credentials: providers, config show (masked), status, rate-limit status and
//! schedule list. Everything else is an honest stub.

pub mod error;
pub mod guard;
pub mod registry;
pub mod server;
pub mod store;
pub mod tools;

pub use error::ToolError;
pub use registry::{registry, Consent, Implementation, Registry, Render, ToolEntry};
pub use server::{XpstMcp, SUPPORTED_PROTOCOL_VERSIONS};
pub use store::StorePaths;
pub use tools::ToolContext;

use rmcp::ServiceExt;

/// Serve the registry over stdio until the client closes the pipe.
///
/// This is the one entry point behind both `xpst mcp start` (and a bare
/// `xpst mcp`, which starts the server in the shipped CLI) and the `xpst-mcp`
/// binary. It is blocking on purpose: the CLI is synchronous, and the runtime
/// belongs to the crate that owns the transport rather than to every caller.
///
/// The banner goes to stderr. stdout is the protocol stream and nothing else
/// may write to it — a stray `println!` here would corrupt the session.
pub fn serve_stdio(ctx: ToolContext) -> Result<(), String> {
    let registry = registry();
    eprintln!(
        "xpst-mcp: serving {} tools ({} ported, {} stubs) from {}",
        registry.len(),
        registry.ported_count(),
        registry.stub_count(),
        ctx.paths.config_dir().display()
    );

    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .map_err(|err| format!("cannot start the async runtime: {err}"))?;

    runtime.block_on(async move {
        let running = match XpstMcp::new(ctx).serve(rmcp::transport::io::stdio()).await {
            Ok(running) => running,
            // A client that closes the pipe before the handshake is a clean
            // shutdown, not a failure: `python -m xpst mcp start < /dev/null`
            // exits 0, and a supervisor that stops the server by closing stdin
            // must not see a crash.
            Err(rmcp::service::ServerInitializeError::ConnectionClosed(_)) => return Ok(()),
            Err(err) => return Err(format!("transport failed: {err}")),
        };
        running
            .waiting()
            .await
            .map_err(|err| format!("transport failed: {err}"))?;
        Ok(())
    })
}
