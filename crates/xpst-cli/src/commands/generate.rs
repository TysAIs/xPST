//! `xpst generate caption` and `xpst generate ideas` — the deterministic
//! content generators.
//!
//! The algorithms live in `xpst_surface::generate`, because the MCP tools
//! `xpst_suggest_caption` and `xpst_generate_ideas` serve the same two
//! functions and `xpst-cli` depends on `xpst-mcp`, never the other way round.
//! This module is only the Click surface and the payload rendering.
//!
//! Parity targets: `src/xpst/caption_gen.py`
//! (`generate_caption_deterministic`, `generate_ideas_deterministic`) and the
//! two Click commands in `src/xpst/cli.py` that wrap them. The payload is
//! rendered with `json.dumps(indent=2, default=str)` — Python's
//! `ensure_ascii=True` default, so a non-ASCII caption comes out as `\uXXXX`
//! and the two engines can be diffed with `cmp`.
//!
//! **Divergence, recorded in the ledger note:** Python routes through
//! `generate_caption_llm`/`generate_ideas_llm` first and only falls back to the
//! deterministic path when no LLM is configured (`XPST_KB_LLM_ENABLED` unset or
//! no base URL). The Rust engine always takes the deterministic path — the LLM
//! client is not ported — so a machine with the knowledge extra and an LLM
//! endpoint configured gets model-written text from Python and template text
//! here. Everything else (the templates, the hashtag table, the character
//! limits, the truncation rules and the payload shape) is the shipped
//! behaviour.

use std::io::Write;

use clap::ArgMatches;
use xpst_config::Value;
use xpst_surface::generate::{caption_variants, char_limit, idea_variants};

use crate::output::{array, integer, object, python_pretty_json_cfg, string, write_text};
use crate::{exit, flag, leaf_string, GlobalOptions};

/// `xpst generate caption`.
pub fn caption(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let text = leaf_string(leaf, "opt:text").unwrap_or_default();
    let platform = leaf_string(leaf, "opt:platform").unwrap_or_else(|| "instagram".to_string());
    let variants = caption_variants(&text, &platform);

    if json_mode {
        let suggestions: Vec<Value> = variants
            .iter()
            .map(|(caption, hashtags, style)| {
                let mut fields = vec![
                    ("caption", string(caption.clone())),
                    ("hashtags", string(hashtags.clone())),
                ];
                if let Some(style) = style {
                    fields.push(("style", string(*style)));
                }
                object(fields)
            })
            .collect();
        let payload = object(vec![
            ("text", string(text)),
            ("platform", string(platform.clone())),
            ("char_limit", integer(char_limit(&platform) as i64)),
            ("suggestions", array(suggestions)),
        ]);
        let _ = writeln!(out, "{}", python_pretty_json_cfg(&payload));
        return exit::SUCCESS;
    }

    let mut rendered = format!("\nCaption suggestions for {platform}:\n\n");
    for (index, (caption, hashtags, style)) in variants.iter().enumerate() {
        rendered.push_str(&format!(
            "  Option {} ({}):\n",
            index + 1,
            style.unwrap_or("default")
        ));
        rendered.push_str(&format!("  {caption}\n"));
        rendered.push_str(&format!("  Hashtags: {hashtags}\n\n"));
    }
    let _ = write_text(out, rendered.trim_end());
    let _ = err;
    exit::SUCCESS
}

/// `xpst generate ideas`.
pub fn ideas(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let topic = leaf_string(leaf, "opt:topic").unwrap_or_default();
    // Click's `IntRange(1, 10)` already rejected anything else; the default is
    // 5 and the clamp inside `generate_ideas_deterministic` is the same rule.
    let count = crate::clickargs::leaf_integer(leaf, "opt:count")
        .unwrap_or(5)
        .clamp(1, 10) as usize;
    let variants = idea_variants(&topic, count);

    if json_mode {
        let rows: Vec<Value> = variants
            .iter()
            .map(|(idea, hook)| {
                object(vec![
                    ("idea", string(idea.clone())),
                    ("hook", string(hook.clone())),
                    ("source", string("deterministic")),
                ])
            })
            .collect();
        let payload = object(vec![
            // The payload echoes the argument, not the defaulted topic.
            ("topic", string(topic.clone())),
            ("count", integer(variants.len() as i64)),
            ("ideas", array(rows)),
        ]);
        let _ = writeln!(out, "{}", python_pretty_json_cfg(&payload));
        return exit::SUCCESS;
    }

    let mut rendered = format!("\nPost ideas for '{topic}':\n\n");
    for (index, (idea, hook)) in variants.iter().enumerate() {
        rendered.push_str(&format!("  {}. {idea}\n", index + 1));
        rendered.push_str(&format!("      hook: {hook} · source: deterministic\n"));
    }
    let _ = write_text(out, rendered.trim_end());
    let _ = err;
    exit::SUCCESS
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The algorithm tests moved to `xpst_surface::generate` with the code.
    /// What stays here is the CLI's own contract: the payload echoes the
    /// argument (not the defaulted topic) and the renderer is `indent=2`.
    #[test]
    fn the_caption_payload_matches_the_shipped_algorithm() {
        let variants = caption_variants(
            "Routing is hard. Concurrency tuning matters a lot for throughput. Learn caching today.",
            "x",
        );
        assert_eq!(variants.len(), 3);
        assert_eq!(
            variants[0].0,
            "Routing is hard Concurrency tuning matters a lot for throughput\n\n#Routing #Hard #Concurrency"
        );
        assert_eq!(variants[0].2, Some("direct"));
        assert_eq!(
            variants[1].0,
            "This changed everything: Concurrency tuning matters a lot for throughput\n\n#Routing #Hard #Concurrency #Tuning"
        );
        assert_eq!(
            variants[2].0,
            "Learn caching today\n\nFollow for more! #Routing #Hard #Concurrency #Tuning #Matters"
        );
    }

    #[test]
    fn a_blank_topic_uses_the_default_but_the_payload_echoes_the_argument() {
        let ideas = idea_variants("   ", 1);
        assert_eq!(
            ideas[0].0,
            "5 things nobody tells you about content creation"
        );
    }
}
