//! The tool registry the code owns.
//!
//! Two invariants hold here, and both are tested:
//!
//! 1. The **names and input schemas are the Python registry's**, never
//!    hand-transcribed. They are captured into
//!    `tests/fixtures/python_tool_registry.json` by
//!    `scripts/capture_mcp_tool_registry.py`; this crate embeds that fixture and
//!    refuses to start if an entry it lists has no Python definition.
//! 2. The **behaviour is stated per tool**. [`Implementation::Ported`] carries
//!    the handler; [`Implementation::Stub`] carries the reason and can only
//!    answer with a typed `not_implemented` error. There is no third state, so
//!    the surface cannot advertise a capability it does not have.
//!
//! Dispatch is a registry lookup, not a match over 46 arms: adding a tool means
//! adding one row to [`ENTRIES`] and (if it is ported) one handler function.

use std::sync::LazyLock;

use serde::Deserialize;
use serde_json::{Map, Value};
use thiserror::Error;

use crate::error::ToolError;
use crate::tools::{Handler, ToolContext};

/// Captured from the shipped Python registry; regenerate, never edit by hand.
const PYTHON_REGISTRY: &str = include_str!("../tests/fixtures/python_tool_registry.json");

/// Whether a call changes state or can publish to a real account.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Consent {
    ReadOnly,
    Mutating,
}

/// How much of a tool this build actually implements.
#[derive(Debug, Clone, Copy)]
pub enum Implementation {
    Ported(Handler),
    Stub { reason: &'static str },
}

/// One row of the surface.
#[derive(Debug)]
pub struct ToolEntry {
    pub name: &'static str,
    pub consent: Consent,
    pub implementation: Implementation,
    spec: ToolSpec,
}

impl ToolEntry {
    pub fn is_ported(&self) -> bool {
        matches!(self.implementation, Implementation::Ported(_))
    }

    pub fn is_read_only(&self) -> bool {
        self.consent == Consent::ReadOnly
    }

    pub fn stub_reason(&self) -> Option<&'static str> {
        match self.implementation {
            Implementation::Ported(_) => None,
            Implementation::Stub { reason } => Some(reason),
        }
    }

    /// The Python definition this row serves.
    pub fn spec(&self) -> &ToolSpec {
        &self.spec
    }
}

/// A tool definition exactly as Python serves it.
#[derive(Debug, Clone)]
pub struct ToolSpec {
    pub name: String,
    pub description: String,
    pub input_schema: Map<String, Value>,
}

#[derive(Debug, Error)]
pub enum RegistryError {
    #[error("the captured Python registry is unreadable: {0}")]
    Fixture(String),

    #[error("tool {name} is listed by the Rust registry but has no Python definition")]
    MissingPythonDefinition { name: &'static str },

    #[error("tool {name} is listed twice")]
    Duplicate { name: &'static str },

    #[error("tool {name} has a non-object input schema in the Python registry")]
    SchemaNotAnObject { name: &'static str },
}

#[derive(Debug, Deserialize)]
struct CapturedTool {
    name: String,
    description: Option<String>,
    input_schema: Value,
}

/// The surface, in Python's order. Names are `&'static str` here so the parity
/// test can compare them with the Python source without allocating.
const ENTRIES: &[(&str, Consent, Implementation)] = &[
    (
        "xpst_run",
        Consent::Mutating,
        Implementation::Stub {
            reason: "runs the posting pipeline, which is not ported",
        },
    ),
    (
        "xpst_post",
        Consent::Mutating,
        Implementation::Stub {
            reason: "publishes through provider adapters, which are not ported",
        },
    ),
    (
        "xpst_analytics",
        Consent::ReadOnly,
        Implementation::Stub {
            reason: "reads analytics.db and can refresh from platform APIs",
        },
    ),
    (
        "xpst_cross_post_analytics",
        Consent::ReadOnly,
        Implementation::Stub {
            reason: "reads analytics.db, which is not ported",
        },
    ),
    (
        "xpst_followers",
        Consent::ReadOnly,
        Implementation::Stub {
            reason: "requires platform APIs",
        },
    ),
    (
        "xpst_best_time",
        Consent::ReadOnly,
        Implementation::Stub {
            reason: "reads engagement history from analytics.db, which is not ported",
        },
    ),
    (
        "xpst_security_audit",
        Consent::ReadOnly,
        Implementation::Stub {
            reason: "runs the Python security scanner, which is not ported",
        },
    ),
    (
        "xpst_suggest_caption",
        Consent::ReadOnly,
        Implementation::Stub {
            reason: "calls the AI caption service",
        },
    ),
    (
        "xpst_generate_ideas",
        Consent::ReadOnly,
        Implementation::Stub {
            reason: "calls the AI content service",
        },
    ),
    (
        "xpst_transcript",
        Consent::ReadOnly,
        Implementation::Stub {
            reason: "reads the knowledge store, which is not ported",
        },
    ),
    (
        "xpst_search",
        Consent::ReadOnly,
        Implementation::Stub {
            reason: "reads the knowledge vector store, which is not ported",
        },
    ),
    (
        "xpst_activity",
        Consent::ReadOnly,
        Implementation::Stub {
            reason: "reads the activity ledger, which is not ported",
        },
    ),
    (
        "xpst_schedule_list",
        Consent::ReadOnly,
        Implementation::Ported(crate::tools::schedule::run),
    ),
    (
        "xpst_schedule_add",
        Consent::Mutating,
        Implementation::Stub {
            reason: "writes the schedule store through Python services, which are not ported",
        },
    ),
    (
        "xpst_schedule_cancel",
        Consent::Mutating,
        Implementation::Stub {
            reason: "mutates the schedule store through Python services, which are not ported",
        },
    ),
    (
        "xpst_schedule_pause",
        Consent::ReadOnly,
        Implementation::Stub {
            reason: "writes the pause marker through Python services, which are not ported",
        },
    ),
    (
        "xpst_schedule_resume",
        Consent::Mutating,
        Implementation::Stub {
            reason: "releases the pause marker through Python services, which are not ported",
        },
    ),
    (
        "xpst_failures_retry",
        Consent::Mutating,
        Implementation::Stub {
            reason: "re-uploads through provider adapters, which are not ported",
        },
    ),
    (
        "xpst_health",
        Consent::ReadOnly,
        Implementation::Stub {
            reason: "probes every platform over the network",
        },
    ),
    (
        "xpst_status",
        Consent::ReadOnly,
        Implementation::Ported(crate::tools::status::run),
    ),
    (
        "xpst_rate_limit_status",
        Consent::ReadOnly,
        Implementation::Ported(crate::tools::rate_limit::run),
    ),
    (
        "xpst_backfill",
        Consent::Mutating,
        Implementation::Stub {
            reason: "re-posts through provider adapters, which are not ported",
        },
    ),
    (
        "xpst_config_show",
        Consent::ReadOnly,
        Implementation::Ported(crate::tools::config_show::run),
    ),
    (
        "xpst_auth_status",
        Consent::ReadOnly,
        Implementation::Stub {
            reason: "probes platform credentials, which needs the network",
        },
    ),
    (
        "xpst_byo_app",
        Consent::ReadOnly,
        Implementation::Stub {
            reason: "reads and decrypts the bring-your-own-app store, which is not ported",
        },
    ),
    (
        "xpst_bio_get",
        Consent::ReadOnly,
        Implementation::Stub {
            reason: "reads the link-in-bio configuration, which is not ported",
        },
    ),
    (
        "xpst_capabilities",
        Consent::ReadOnly,
        Implementation::Stub {
            reason: "provider state is credential-derived, which needs platform probes",
        },
    ),
    (
        "xpst_preflight",
        Consent::ReadOnly,
        Implementation::Stub {
            reason: "resolves destinations against live credentials and the content contract",
        },
    ),
    (
        "xpst_readiness",
        Consent::ReadOnly,
        Implementation::Stub {
            reason: "probes credentials, helper binaries and directories",
        },
    ),
    (
        "xpst_auth_start",
        Consent::ReadOnly,
        Implementation::Stub {
            reason: "builds the per-platform auth plan from the auth flow, which is not ported",
        },
    ),
    (
        "xpst_providers",
        Consent::ReadOnly,
        Implementation::Ported(crate::tools::providers::run),
    ),
    (
        "xpst_disconnect",
        Consent::Mutating,
        Implementation::Stub {
            reason: "removes stored credentials through Python services, which are not ported",
        },
    ),
    (
        "xpst_delete",
        Consent::Mutating,
        Implementation::Stub {
            reason: "deletes posts on the platform, which needs provider adapters",
        },
    ),
    (
        "messenger_send",
        Consent::Mutating,
        Implementation::Stub {
            reason: "sends through the Meta Graph API",
        },
    ),
    (
        "messenger_set_rules",
        Consent::Mutating,
        Implementation::Stub {
            reason:
                "writes the Messenger rules through Python config services, which are not ported",
        },
    ),
    (
        "xpst_messenger_check_comments",
        Consent::Mutating,
        Implementation::Stub {
            reason: "reads and replies on the Meta Graph API",
        },
    ),
    (
        "kb_add",
        Consent::Mutating,
        Implementation::Stub {
            reason: "ingests into the knowledge store, which is not ported",
        },
    ),
    (
        "kb_query",
        Consent::ReadOnly,
        Implementation::Stub {
            reason: "reads the knowledge vector store, which is not ported",
        },
    ),
    (
        "kb_organize",
        Consent::Mutating,
        Implementation::Stub {
            reason: "writes the knowledge store, which is not ported",
        },
    ),
    (
        "kb_areas",
        Consent::ReadOnly,
        Implementation::Stub {
            reason: "reads the knowledge store, which is not ported",
        },
    ),
    (
        "xpst_setup_start",
        Consent::ReadOnly,
        Implementation::Stub {
            reason:
                "writes the setup transaction file through Python services, which are not ported",
        },
    ),
    (
        "xpst_setup_status",
        Consent::ReadOnly,
        Implementation::Stub {
            reason: "reads the setup transaction service, which is not ported",
        },
    ),
    (
        "xpst_setup_resume",
        Consent::ReadOnly,
        Implementation::Stub {
            reason:
                "writes the setup transaction file through Python services, which are not ported",
        },
    ),
    (
        "xpst_setup_reset",
        Consent::ReadOnly,
        Implementation::Stub {
            reason:
                "removes the setup transaction file through Python services, which are not ported",
        },
    ),
    (
        "xpst_stream",
        Consent::ReadOnly,
        Implementation::Stub {
            reason: "reads the stream registry and can probe live channels",
        },
    ),
    (
        "xpst_chat",
        Consent::ReadOnly,
        Implementation::Stub {
            reason: "opens live chat transports over the network",
        },
    ),
];

/// A built registry: every entry paired with the Python definition it serves.
#[derive(Debug)]
pub struct Registry {
    entries: Vec<ToolEntry>,
}

impl Registry {
    pub fn build() -> Result<Self, RegistryError> {
        let captured: Vec<CapturedTool> = serde_json::from_str(PYTHON_REGISTRY)
            .map_err(|err| RegistryError::Fixture(err.to_string()))?;

        let mut entries: Vec<ToolEntry> = Vec::with_capacity(ENTRIES.len());
        for (name, consent, implementation) in ENTRIES {
            if entries.iter().any(|entry| entry.name == *name) {
                return Err(RegistryError::Duplicate { name });
            }
            let tool = captured
                .iter()
                .find(|tool| tool.name == *name)
                .ok_or(RegistryError::MissingPythonDefinition { name })?;
            let input_schema = tool
                .input_schema
                .as_object()
                .cloned()
                .ok_or(RegistryError::SchemaNotAnObject { name })?;
            entries.push(ToolEntry {
                name,
                consent: *consent,
                implementation: *implementation,
                spec: ToolSpec {
                    name: tool.name.clone(),
                    description: tool.description.clone().unwrap_or_default(),
                    input_schema,
                },
            });
        }
        Ok(Self { entries })
    }

    pub fn entries(&self) -> &[ToolEntry] {
        &self.entries
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    pub fn get(&self, name: &str) -> Option<&ToolEntry> {
        self.entries.iter().find(|entry| entry.name == name)
    }

    pub fn ported_count(&self) -> usize {
        self.entries
            .iter()
            .filter(|entry| entry.is_ported())
            .count()
    }

    pub fn stub_count(&self) -> usize {
        self.entries.len() - self.ported_count()
    }

    /// The MCP `tools/list` payload, built from the registry rows.
    pub fn tools(&self) -> Vec<rmcp::model::Tool> {
        self.entries
            .iter()
            .map(|entry| {
                rmcp::model::Tool::new(
                    entry.name,
                    entry.spec.description.clone(),
                    entry.spec.input_schema.clone(),
                )
            })
            .collect()
    }

    /// Route one call. Ported tools execute; stubs answer with a typed
    /// `not_implemented`; unknown names are told they are unknown.
    pub fn dispatch(
        &self,
        ctx: &ToolContext,
        name: &str,
        arguments: &Value,
    ) -> Result<Value, ToolError> {
        let entry = self.get(name).ok_or_else(|| ToolError::UnknownTool {
            name: name.to_string(),
        })?;
        match entry.implementation {
            Implementation::Ported(handler) => handler(ctx, arguments),
            Implementation::Stub { reason } => Err(ToolError::NotImplemented {
                tool: entry.name,
                reason,
            }),
        }
    }
}

/// The process-wide registry. A build failure is a programming error (a row
/// with no Python definition), so it panics with the offending name instead of
/// silently serving a shorter surface.
pub fn registry() -> &'static Registry {
    static REGISTRY: LazyLock<Registry> = LazyLock::new(|| {
        Registry::build().unwrap_or_else(|err| panic!("invalid MCP tool registry: {err}"))
    });
    &REGISTRY
}
