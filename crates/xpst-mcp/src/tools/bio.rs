//! `xpst_bio_get` — the link-in-bio URL and its current configuration.
//!
//! Ports `_handle_bio_get()`: the public `/bio` URL (the dashboard's default
//! bind), the page handle, and the ordered links exactly as the dashboard
//! renders them. The URL is a constant in the Python handler, not a probe of a
//! running server, so it is a constant here too.

use serde_json::{json, Value};

use crate::error::ToolError;

use super::ToolContext;

pub fn run(ctx: &ToolContext, _arguments: &Value) -> Result<Value, ToolError> {
    let view = ctx.config()?;
    Ok(json!({
        "url": "http://127.0.0.1:8080/bio",
        "handle": xpst_surface::bio::handle(view),
        "links": xpst_surface::bio::collect_links(view),
    }))
}
