//! `xpst_capabilities` — the canonical role-aware provider catalog plus the
//! content contract.
//!
//! Ports `_handle_capabilities()`: `canonical_provider_catalog(config)` projected
//! down to six fields per provider, plus `capability_document()` verbatim. No
//! network call and no live probe — the catalog is the offline view, so every
//! live-derived fact is `null` ("not probed") rather than `false`.
//!
//! The projection is the tool's own, not the CLI's: `xpst providers` serves the
//! whole catalog entry (role statuses, posting truth, probe fields, BYO app),
//! while this tool serves `name`/`display_name`/`roles`/`capabilities`/`state`/
//! `docs_url` and the content contract. Both read the same
//! `xpst_surface::catalog::catalog_entry`, so the two cannot disagree about a
//! provider's state.

use serde_json::{json, Value};
use xpst_auth::credentials::CredentialStore;

use crate::error::ToolError;

use super::ToolContext;

pub fn run(ctx: &ToolContext, _arguments: &Value) -> Result<Value, ToolError> {
    let view = ctx.config()?;
    let store = CredentialStore::new(ctx.paths.config_dir());
    let registry =
        xpst_surface::provider_status::registry().map_err(|detail| ToolError::Malformed {
            kind: "provider registry",
            path: String::new(),
            detail,
        })?;

    let providers: Vec<Value> = registry
        .definitions
        .iter()
        .map(|definition| {
            let entry = xpst_config::output::to_json(&xpst_surface::catalog::catalog_entry(
                &registry, view, &store, definition,
            ));
            json!({
                "name": definition.name,
                "display_name": definition.display_name,
                "roles": definition.roles,
                "capabilities": entry.get("capabilities").cloned().unwrap_or_else(|| json!([])),
                "state": entry.get("state").cloned().unwrap_or(Value::Null),
                "docs_url": definition.docs_url,
            })
        })
        .collect();

    let content =
        xpst_surface::capabilities::document().map_err(|detail| ToolError::Malformed {
            kind: "capability document",
            path: String::new(),
            detail,
        })?;

    Ok(json!({
        "ok": true,
        "contract_version": 1,
        "roles": registry.roles,
        "providers": providers,
        // The content contract, verbatim from its one source, so the list an
        // agent plans against is the list the CLI prints and the HTTP API
        // serves — not a per-surface copy.
        "content": content,
    }))
}
