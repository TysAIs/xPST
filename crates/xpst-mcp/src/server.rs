//! The MCP server binding: rmcp's `ServerHandler` over the registry.
//!
//! No tool is described here. `list_tools` renders the registry, `call_tool`
//! asks the registry, and the consent guard runs before any handler — the
//! three things a caller can observe are each owned by exactly one module.

use rmcp::handler::server::ServerHandler;
use rmcp::model::{
    CallToolRequestParams, CallToolResponse, CallToolResult, ContentBlock,
    Implementation as McpImplementation, ListToolsResult, PaginatedRequestParams, ProtocolVersion,
    ServerCapabilities, ServerConfig,
};
use rmcp::service::RequestContext;
use rmcp::{ErrorData as McpError, RoleServer};
use serde_json::Value;

use crate::guard::ConsentPolicy;
use crate::registry::{registry, Registry};
use crate::tools::ToolContext;

/// Server name reported in `initialize`, matching the Python server.
pub const SERVER_NAME: &str = "xpst-mcp";

/// The MCP revisions this server will negotiate, oldest first.
///
/// These are exactly the protocol generations whose lifecycle is a
/// `initialize` handshake. That is not a preference: every test in this crate
/// drives the server over that handshake, and the tool surface is stateless
/// (no sessions, no elicitation, no sampling, no server-initiated requests),
/// so the newer inline lifecycle buys nothing this server exposes. Declaring
/// the list explicitly means a future rmcp bump cannot silently widen what
/// xPST claims to speak — widening it is a diff someone has to publish, with a
/// test behind it.
///
/// A client that opens with `initialize` and asks for a newer revision is
/// answered with the newest entry here instead of an error, which is the SDK's
/// designed fallback for a pinned server. That covers the handshake path only:
/// a client that speaks just the newer inline lifecycle never sends
/// `initialize`, so its requests are refused with `-32022 Unsupported protocol
/// version` naming this list rather than served silently. Both paths are pinned
/// in `crates/xpst-mcp/tests/stdio_smoke.rs`.
pub const SUPPORTED_PROTOCOL_VERSIONS: &[ProtocolVersion] = &[
    ProtocolVersion::V_2024_11_05,
    ProtocolVersion::V_2025_03_26,
    ProtocolVersion::V_2025_06_18,
    ProtocolVersion::V_2025_11_25,
];

pub struct XpstMcp {
    registry: &'static Registry,
    ctx: ToolContext,
    policy: Option<ConsentPolicy>,
}

impl XpstMcp {
    pub fn new(ctx: ToolContext) -> Self {
        Self {
            registry: registry(),
            ctx,
            policy: None,
        }
    }

    /// Pin the consent policy instead of reading the environment per call
    /// (tests, embedders).
    pub fn with_policy(mut self, policy: ConsentPolicy) -> Self {
        self.policy = Some(policy);
        self
    }

    pub fn tool_context(&self) -> &ToolContext {
        &self.ctx
    }

    pub fn registry(&self) -> &'static Registry {
        self.registry
    }

    fn policy(&self) -> ConsentPolicy {
        self.policy.unwrap_or_else(ConsentPolicy::from_env)
    }
}

impl ServerHandler for XpstMcp {
    fn get_info(&self) -> ServerConfig {
        ServerConfig::new(ServerCapabilities::builder().enable_tools().build()).with_server_info(
            McpImplementation::new(SERVER_NAME, env!("CARGO_PKG_VERSION")),
        )
    }

    fn supported_protocol_versions(&self) -> std::borrow::Cow<'static, [ProtocolVersion]> {
        std::borrow::Cow::Borrowed(SUPPORTED_PROTOCOL_VERSIONS)
    }

    async fn list_tools(
        &self,
        _request: Option<PaginatedRequestParams>,
        _context: RequestContext<RoleServer>,
    ) -> Result<ListToolsResult, McpError> {
        Ok(ListToolsResult::with_all_items(self.registry.tools()))
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        _context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, McpError> {
        let name = request.name.as_ref();
        let arguments = request
            .arguments
            .map(Value::Object)
            .unwrap_or(Value::Object(Default::default()));

        let entry = match self.registry.get(name) {
            Some(entry) => entry,
            None => {
                return Ok(error_result(&crate::error::ToolError::UnknownTool {
                    name: name.to_string(),
                })
                .into())
            }
        };

        let policy = self.policy();
        if let Err(denied) = policy
            .permits(entry)
            .and_then(|()| policy.confirms(entry, &arguments))
        {
            return Ok(error_result(&denied).into());
        }

        // `CallToolResponse::Complete` is the only shape this server produces:
        // it never asks for client input and never materializes a task, and the
        // wire form of `Complete` is exactly the `CallToolResult` Python serves.
        match self.registry.dispatch(&self.ctx, name, &arguments) {
            Ok(value) => Ok(success_result(&value).into()),
            Err(err) => Ok(error_result(&err).into()),
        }
    }
}

fn success_result(value: &Value) -> CallToolResult {
    // Python serves a pretty-printed JSON document as text content; keeping the
    // same body means an agent's prompt does not change shape between servers.
    let text = serde_json::to_string_pretty(value).unwrap_or_else(|_| "null".to_string());
    CallToolResult::success(vec![ContentBlock::text(text)])
}

fn error_result(err: &crate::error::ToolError) -> CallToolResult {
    CallToolResult::error(vec![ContentBlock::text(err.to_payload().to_string())])
}
