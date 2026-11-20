//! `xpst-mcp` — the MCP server over stdio.
//!
//! Speaks MCP on stdin/stdout (never logs there; diagnostics go to stderr) and
//! serves the registry described in [`xpst_mcp::registry`].

use rmcp::transport::io::stdio;
use rmcp::ServiceExt;
use xpst_mcp::{ToolContext, XpstMcp};

#[tokio::main]
async fn main() -> std::process::ExitCode {
    let ctx = match ToolContext::discover() {
        Ok(ctx) => ctx,
        Err(err) => {
            eprintln!("xpst-mcp: {err}");
            return std::process::ExitCode::from(1);
        }
    };

    let server = XpstMcp::new(ctx.clone());
    eprintln!(
        "xpst-mcp: serving {} tools ({} ported, {} stubs) from {}",
        server.registry().len(),
        server.registry().ported_count(),
        server.registry().stub_count(),
        ctx.paths.config_dir().display()
    );

    match server.serve(stdio()).await {
        Ok(running) => {
            let _ = running.waiting().await;
            std::process::ExitCode::SUCCESS
        }
        Err(err) => {
            eprintln!("xpst-mcp: transport failed: {err}");
            std::process::ExitCode::from(1)
        }
    }
}
