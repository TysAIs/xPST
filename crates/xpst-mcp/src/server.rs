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
use crate::registry::{registry, Registry, Render};
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
        // Python's SDK advertises `{"tools": {"listChanged": false}}`; rmcp's
        // `enable_tools()` leaves the field out entirely, which is a different
        // wire document for the same capability. The server never sends
        // `notifications/tools/list_changed` (the surface is static), so `false`
        // is the honest value and the one a Python-trained client already sees.
        let mut capabilities = ServerCapabilities::builder().enable_tools().build();
        if let Some(tools) = capabilities.tools.as_mut() {
            tools.list_changed = Some(false);
        }
        ServerConfig::new(capabilities).with_server_info(McpImplementation::new(
            SERVER_NAME,
            env!("CARGO_PKG_VERSION"),
        ))
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

        // Validate path arguments for EVERY tool (not just mutating ones) before
        // anything touches the filesystem: an agent — or an LLM reading
        // untrusted content — chooses these. Python runs this after the consent
        // guard and before dispatch, and the refusal is a bare sentence.
        if let Some(message) =
            crate::guard::argument_shape_block(name, &arguments, self.ctx.paths.config_dir())
        {
            return Ok(error_result(&crate::error::ToolError::Reported { message }).into());
        }

        // `CallToolResponse::Complete` is the only shape this server produces:
        // it never asks for client input and never materializes a task, and the
        // wire form of `Complete` is exactly the `CallToolResult` Python serves.
        match self.registry.dispatch(&self.ctx, name, &arguments) {
            Ok(value) => {
                let render = entry.render().unwrap_or(Render::Pretty);
                Ok(success_result(&value, render).into())
            }
            Err(err) => Ok(error_result(&err).into()),
        }
    }
}

/// Render a ported tool's payload the way its Python handler renders it.
///
/// The body is the tool's own `json.dumps` call, not a server-wide policy: the
/// shipped handlers disagree (`indent=2` for most, compact for
/// `xpst_schedule_list`), and an agent that parses the text — rather than
/// re-serialising it — sees the difference. [`Render`] is pinned per registry
/// row for exactly that reason.
fn success_result(value: &Value, render: Render) -> CallToolResult {
    let text = match render {
        Render::Pretty => xpst_config::python_style_json_pretty(value),
        Render::PrettySorted => {
            let mut sorted = value.clone();
            xpst_core::json::sort_keys_recursive(&mut sorted);
            xpst_config::python_style_json_pretty(&sorted)
        }
        Render::Compact => xpst_config::python_style_json_compact(value),
    }
    .unwrap_or_else(|_| "null".to_string());
    CallToolResult::success(vec![ContentBlock::text(text)])
}

/// A failure is plain text, because that is what Python serves.
///
/// `_guardrail_block` returns the sentence itself, an unknown name answers
/// `Unknown tool: <name>`, and a handler that raises answers `Error: <message>`
/// — all with `isError: true` and no JSON envelope. An agent that already
/// handles the Python server's refusals therefore handles these unchanged.
fn error_result(err: &crate::error::ToolError) -> CallToolResult {
    CallToolResult::error(vec![ContentBlock::text(err.to_string())])
}
