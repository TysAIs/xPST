//! `xpst preflight` — the resolved per-destination plan, before anything is
//! published.
//!
//! Parity target: `src/xpst/cli.py::preflight_cmd` →
//! `xpst.services.route_plan.build_route_plan` →
//! `xpst.services.post_preflight.PostPreflightService.plan`.
//!
//! The host and the payload builders live in `xpst_surface::preflight`, because
//! the `xpst_preflight` MCP tool serves the same documents. This module is the
//! clap surface plus the plain-text renderer.
//!
//! **Divergence, recorded in the ledger note:** text mode prints plain lines
//! instead of Rich's coloured output (`--json` is the parity surface, as for
//! every other ported command).

use std::io::Write;
use std::path::{Path, PathBuf};

use clap::ArgMatches;
use xpst_core::preflight::{self, PlanRequest, TransformPlan};

use crate::commands::bootstrap;
use crate::commands::config_view::ConfigView;
use crate::output::{write_python_json, write_text};
use crate::{exit, flag, GlobalOptions};

pub use xpst_surface::preflight::{
    constraints_value, enabled_destinations, issue_value, media_file_value, media_spec_value,
    plan_value, platform_enabled, platform_plan_value, post_plan_value, preflight_issue_value,
    readiness_value, transform_value, Host, DEFAULT_ENABLED, DESTINATION_ORDER,
};

pub fn run(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let media: Vec<String> = leaf
        .and_then(|matches| matches.get_many::<String>("arg:media"))
        .map(|values| values.cloned().collect())
        .unwrap_or_default();
    let caption = crate::leaf_string(leaf, "opt:caption").unwrap_or_default();
    let text = crate::leaf_string(leaf, "opt:text");
    let format = crate::leaf_string(leaf, "opt:format").unwrap_or_else(|| "auto".to_owned());
    let platforms = crate::leaf_string(leaf, "opt:platforms");
    let all_connected = leaf.is_some_and(|m| flag(m, "opt:all"));
    let show_transform = leaf.is_some_and(|m| flag(m, "opt:transform"));
    let visibility = crate::leaf_string(leaf, "opt:visibility");

    let platform_list = platforms
        .as_deref()
        .map(|value| value.split(',').map(str::to_owned).collect::<Vec<_>>());
    if platform_list.is_some() && all_connected {
        let _ = writeln!(err, "Error: Use either --platforms or --all, not both.");
        return exit::CONFIG_ERROR;
    }

    let config_path = match bootstrap::ensure_config(global) {
        Ok(path) => path,
        Err(error) => return bootstrap::report(out, err, &error),
    };
    let document = match xpst_config::ConfigDocument::read(&config_path) {
        Ok(document) => document.raw().clone(),
        Err(error) => {
            let _ = writeln!(err, "{error}");
            return exit::CONFIG_ERROR;
        }
    };
    let config_dir = config_path
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."));
    let env = |name: &str| std::env::var(name).ok();
    let view = match ConfigView::load(&document, &config_dir, &env) {
        Ok(view) => view,
        Err(message) => {
            let _ = writeln!(err, "xpst preflight: {message}");
            return exit::GENERAL;
        }
    };

    let body = text.clone().unwrap_or_else(|| caption.clone());
    let content_type = if format == "auto" {
        if text.is_some() {
            Some("text".to_owned())
        } else {
            Some("auto".to_owned())
        }
    } else {
        Some(format.clone())
    };

    let host = Host::new(&view, &config_dir, &env);
    let request = PlanRequest {
        media_paths: media,
        caption: body,
        per_platform_captions: Vec::new(),
        content_type,
        requested: platform_list,
        all_connected,
        include_transform: show_transform,
        include_readiness: true,
        probe_media: true,
        visibility,
        // `build_route_plan` pins this off, so the route plan never spawns the
        // loudness pass; `verify-media` is the command that turns it on.
        check_loudness: false,
    };
    let plan = preflight::build_route_plan(&host, &request);
    let payload = plan_value(&plan);

    if json_mode {
        let _ = write_python_json(out, &payload);
    } else {
        let mut text_out = String::from("Preflight — resolved route\n");
        text_out.push_str(&format!(
            "  Format: {}   Target source: {}   Network calls: none\n",
            plan.content_type.as_deref().unwrap_or("None"),
            plan.source
        ));
        if plan.destinations.is_empty() {
            text_out.push_str("  No destination resolved.\n");
        }
        for item in &plan.destinations {
            text_out.push_str(&format!(
                "  • {}: {} — {}\n",
                item.platform, item.state, item.reason
            ));
            for issue in &item.warnings {
                text_out.push_str(&format!(
                    "      advisory ({}): {}\n",
                    issue.code, issue.message
                ));
            }
            let plans: Vec<&TransformPlan> = if item.transforms.is_empty() {
                item.transform.iter().collect()
            } else {
                item.transforms.iter().collect()
            };
            for (index, transform) in plans.iter().enumerate() {
                let prefix = if plans.len() > 1 {
                    format!("item {} ", index + 1)
                } else {
                    String::new()
                };
                text_out.push_str(&format!(
                    "      {prefix}{}: {} ({})\n",
                    transform.platform,
                    transform.action,
                    transform.reasons.join("; ")
                ));
            }
        }
        if let Some(error) = plan.actionable_error() {
            text_out.push_str(&format!("  {}: {}\n", error.code, error.message));
        }
        let posting = plan.posting_destinations();
        if !posting.is_empty() {
            text_out.push_str(&format!("Would publish to: {}\n", posting.join(", ")));
        } else if plan.actionable_error().is_some() {
            text_out.push_str("  Nothing would be published.\n");
        } else {
            text_out.push_str("Nothing would be published to any destination.\n");
        }
        let _ = write_text(out, &text_out);
    }

    if plan.actionable_error().is_some() {
        return exit::CONFIG_ERROR;
    }
    if plan.posting_destinations().is_empty() {
        return exit::GENERAL;
    }
    exit::SUCCESS
}
