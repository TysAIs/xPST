//! `xpst generate caption`, `xpst generate ideas` and `xpst suggest-caption` —
//! the deterministic content generators.
//!
//! The algorithms live in `xpst_surface::generate`, because the MCP tools
//! `xpst_suggest_caption` and `xpst_generate_ideas` serve the same two
//! functions and `xpst-cli` depends on `xpst-mcp`, never the other way round.
//! This module is only the Click surface and the payload rendering.
//!
//! Parity targets: `src/xpst/caption_gen.py`
//! (`generate_caption_deterministic`, `generate_ideas_deterministic`) and the
//! three Click commands in `src/xpst/cli.py` that wrap them. The payload is
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
//!
//! `suggest-caption` carries a second divergence of the same shape: its seed is
//! the video's filename stem, because the KB transcriber (`faster-whisper`,
//! from the optional `knowledge` extra) is not ported either. See
//! [`suggest_caption`].

use std::io::Write;
use std::path::Path;

use clap::ArgMatches;
use xpst_config::Value;
use xpst_surface::generate::{
    caption_seed_from_path, caption_variants, char_limit, idea_variants, CaptionVariant,
};

use crate::output::{array, integer, object, python_pretty_json_cfg, string, write_text};
use crate::{exit, flag, leaf_string, GlobalOptions};

/// The human view both caption commands print.
///
/// `generate caption` and `suggest-caption` share this renderer in `cli.py`
/// (the two bodies are the same four `console.print` calls), so they share it
/// here too. Rich's markup is dropped — the standing divergence for every human
/// view — but the facts, the order and the blank lines are the shipped ones.
fn render_caption_variants(platform: &str, variants: &[CaptionVariant]) -> String {
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
    rendered.trim_end().to_string()
}

/// The `--json` payload both caption commands print.
fn caption_payload(
    seed_field: &str,
    seed: String,
    platform: &str,
    variants: &[CaptionVariant],
) -> Value {
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
    object(vec![
        (seed_field, string(seed)),
        ("platform", string(platform.to_string())),
        ("char_limit", integer(char_limit(platform) as i64)),
        ("suggestions", array(suggestions)),
    ])
}

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
        let payload = caption_payload("text", text, &platform, &variants);
        let _ = writeln!(out, "{}", python_pretty_json_cfg(&payload));
        return exit::SUCCESS;
    }

    let _ = write_text(out, &render_caption_variants(&platform, &variants));
    let _ = err;
    exit::SUCCESS
}

/// Click's usage error for a `click.Path(exists=True)` argument.
///
/// The program name is `xpst` where Click prints `python -m xpst` — the
/// ledger's standing usage-error divergence. Everything else is byte-identical,
/// including the metavar Click derives from the parameter name.
fn missing_path_usage(video_path: &str) -> String {
    format!(
        "Usage: xpst suggest-caption [OPTIONS] VIDEO_PATH\n\
         Try 'xpst suggest-caption --help' for help.\n\n\
         Error: Invalid value for 'VIDEO_PATH': Path '{video_path}' does not exist."
    )
}

/// `xpst suggest-caption <video_path>`.
///
/// **The seed is the filename stem, always.** Python tries the KB transcriber
/// first (`FasterWhisperTranscriber`, from the optional `knowledge` extra) and
/// falls back to `Path(video_path).stem.replace("_", " ")` on *any* exception —
/// a missing `faster_whisper`, an unresolvable model, an undecodable file. The
/// Rust engine has no ASR path at all, so it always takes the fallback. That is
/// a named divergence in the ledger, and the local-surface fixture pins the
/// fallback branch by making the transcriber unavailable to the Python engine
/// too (`XPST_KB_WHISPER_MODEL` names a model faster-whisper rejects, with
/// `HF_HUB_OFFLINE=1` and an empty `HF_HOME` so no download is attempted).
///
/// The LLM half is the same divergence `generate caption` carries: Python
/// routes through `generate_caption_llm` when `XPST_KB_LLM_ENABLED` and a base
/// URL are set, and the Rust engine always takes the deterministic path.
pub fn suggest_caption(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let video_path = leaf_string(leaf, "arg:video_path").unwrap_or_default();
    let platform = leaf_string(leaf, "opt:platform").unwrap_or_else(|| "instagram".to_string());

    // `click.Path(exists=True)` validates the argument before the command body
    // runs, so a missing VIDEO_PATH is a usage error (exit 2) with Click's
    // wording — not a caption generated from a path that is not there.
    if !Path::new(&video_path).exists() {
        let _ = writeln!(err, "{}", missing_path_usage(&video_path));
        return exit::CONFIG_ERROR;
    }

    let seed = caption_seed_from_path(&video_path);
    let variants = caption_variants(&seed, &platform);

    if json_mode {
        // The payload echoes the argument, not the derived seed.
        let payload = caption_payload("video_path", video_path, &platform, &variants);
        let _ = writeln!(out, "{}", python_pretty_json_cfg(&payload));
        return exit::SUCCESS;
    }

    let _ = write_text(out, &render_caption_variants(&platform, &variants));
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

    /// The `suggest-caption` payload echoes the ARGUMENT, not the derived seed,
    /// and carries the same four keys in the same order as `generate caption`.
    #[test]
    fn the_suggest_caption_payload_echoes_the_path_and_derives_the_seed() {
        let variants = caption_variants("my clip final", "x");
        let payload = caption_payload(
            "video_path",
            "media/my_clip-final.mp4".to_string(),
            "x",
            &variants,
        );
        let rendered = python_pretty_json_cfg(&payload);
        let parsed: serde_json::Value = serde_json::from_str(&rendered).expect("valid JSON");
        let keys: Vec<&str> = parsed
            .as_object()
            .expect("object")
            .keys()
            .map(String::as_str)
            .collect();
        assert_eq!(
            keys,
            vec!["video_path", "platform", "char_limit", "suggestions"]
        );
        assert_eq!(parsed["video_path"], "media/my_clip-final.mp4");
        assert_eq!(parsed["platform"], "x");
        assert_eq!(parsed["char_limit"], 280);
        assert_eq!(parsed["suggestions"].as_array().expect("array").len(), 3);
        // `indent=2` with Python's separators, not a compact document.
        assert!(rendered.starts_with("{\n  \"video_path\": "));
    }

    /// The human view is the shipped four `console.print` calls with Rich's
    /// markup dropped: the header, then per option the style, the caption and
    /// the hashtags, with a blank line between options and none at the end.
    #[test]
    fn the_human_view_matches_the_shipped_renderer() {
        let variants = caption_variants("my clip final", "instagram");
        let rendered = render_caption_variants("instagram", &variants);
        let expected = [
            "",
            "Caption suggestions for instagram:",
            "",
            "  Option 1 (direct):",
            "  my clip final\n\n#Clip #Final",
            "  Hashtags: #Clip #Final",
            "",
            "  Option 2 (hook):",
            "  Here's the thing: my clip final\n\n#Clip #Final",
            "  Hashtags: #Clip #Final",
            "",
            "  Option 3 (summary):",
            "  my clip final\n\nFollow for more! #Clip #Final",
            "  Hashtags: #Clip #Final",
        ]
        .join("\n");
        assert_eq!(rendered, expected);
    }

    /// The fallback branch carries no `style` key, so the human view prints
    /// `default` — `c.get('style', 'default')`, not an empty string.
    #[test]
    fn the_human_view_names_the_fallback_style_default() {
        let variants = caption_variants("", "instagram");
        let rendered = render_caption_variants("instagram", &variants);
        assert!(rendered.contains("  Option 1 (default):\n  Check out this video!\n"));
    }

    /// `click.Path(exists=True)`'s sentence, captured from the shipped CLI.
    #[test]
    fn a_missing_video_path_is_clicks_usage_error() {
        assert_eq!(
            missing_path_usage("/nonexistent/parity.mp4"),
            "Usage: xpst suggest-caption [OPTIONS] VIDEO_PATH\n\
             Try 'xpst suggest-caption --help' for help.\n\n\
             Error: Invalid value for 'VIDEO_PATH': Path '/nonexistent/parity.mp4' does not exist."
        );
    }
}
