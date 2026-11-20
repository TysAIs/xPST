//! `xpst_providers` — supported sources and destinations.
//!
//! The catalog is provider **metadata** (names, roles, capabilities, docs
//! links): no credentials, no network, no per-install state. It is captured
//! from the shipped Python registry by
//! `scripts/capture_mcp_tool_registry.py` and checked in, so this tool cannot
//! drift into a hand-maintained second copy of the provider list.

use serde_json::Value;
use std::sync::LazyLock;

use crate::error::ToolError;

use super::ToolContext;

/// Captured from `build_provider_catalog()` against an isolated empty profile.
const PYTHON_CATALOG: &str = include_str!("../../tests/fixtures/python_providers.json");

static CATALOG: LazyLock<Result<Value, String>> =
    LazyLock::new(|| serde_json::from_str(PYTHON_CATALOG).map_err(|err| err.to_string()));

pub fn run(_ctx: &ToolContext, _arguments: &Value) -> Result<Value, ToolError> {
    match &*CATALOG {
        Ok(value) => Ok(value.clone()),
        Err(detail) => Err(ToolError::Malformed {
            kind: "provider catalog fixture",
            path: "tests/fixtures/python_providers.json".to_string(),
            detail: detail.clone(),
        }),
    }
}
