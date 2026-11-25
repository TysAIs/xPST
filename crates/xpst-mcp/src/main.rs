//! `xpst-mcp` — the MCP server over stdio.
//!
//! Speaks MCP on stdin/stdout (never logs there; diagnostics go to stderr) and
//! serves the registry described in [`xpst_mcp::registry`]. The serving loop
//! lives in [`xpst_mcp::serve_stdio`] so this binary and `xpst mcp start` are
//! the same server, not two implementations of one.

use xpst_mcp::{serve_stdio, ToolContext};

fn main() -> std::process::ExitCode {
    let ctx = match ToolContext::discover() {
        Ok(ctx) => ctx,
        Err(err) => {
            eprintln!("xpst-mcp: {err}");
            return std::process::ExitCode::from(1);
        }
    };

    match serve_stdio(ctx) {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("xpst-mcp: {err}");
            std::process::ExitCode::from(1)
        }
    }
}
