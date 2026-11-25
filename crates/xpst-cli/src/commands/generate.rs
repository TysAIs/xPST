//! `xpst generate caption` and `xpst generate ideas` — the deterministic
//! content generators.
//!
//! Parity targets: `src/xpst/caption_gen.py`
//! (`generate_caption_deterministic`, `generate_ideas_deterministic`,
//! `PLATFORM_CHAR_LIMITS`, `HASHTAG_SUGGESTIONS`, `IDEA_TEMPLATES`,
//! `IDEA_HOOKS`) and the two Click commands in `src/xpst/cli.py` that wrap
//! them.
//!
//! Both commands are pure string algorithms: no config, no store, no network.
//! The payload is rendered with `json.dumps(indent=2, default=str)` — Python's
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
//! Two Python details a hand-port gets wrong, both reproduced deliberately:
//!
//! * `re.split(r"[.!?]+", text)` splits on a *run* of terminators, so
//!   `"a..b"` yields `["a", "b"]` — not an empty sentence between them.
//! * `re.findall(r"\b[a-zA-Z]{4,}\b", text.lower())` only matches a letter run
//!   that is bounded by non-word characters, so `"abc123def"` contributes no
//!   keyword at all (the digits are word characters, so neither `abc` nor `def`
//!   has a boundary on both sides). A naive "runs of letters" scan invents
//!   hashtags Python never produces.

use std::io::Write;

use clap::ArgMatches;
use xpst_config::Value;

use crate::output::{array, integer, object, python_pretty_json_cfg, string, write_text};
use crate::{exit, flag, leaf_string, GlobalOptions};

/// `PLATFORM_CHAR_LIMITS` — anything else falls back to 2200.
const PLATFORM_CHAR_LIMITS: &[(&str, usize)] = &[
    ("x", 280),
    ("threads", 500),
    ("instagram", 2200),
    ("youtube", 5000),
    ("tiktok", 2200),
];

/// `HASHTAG_SUGGESTIONS`, in the shipped order.
const HASHTAG_SUGGESTIONS: &[(&str, &[&str])] = &[
    (
        "ai",
        &[
            "#AI",
            "#ArtificialIntelligence",
            "#MachineLearning",
            "#TechTrends",
        ],
    ),
    (
        "tech",
        &["#Tech", "#Technology", "#Innovation", "#TechTips"],
    ),
    (
        "business",
        &["#Business", "#Entrepreneur", "#Startup", "#BusinessTips"],
    ),
    (
        "marketing",
        &[
            "#Marketing",
            "#DigitalMarketing",
            "#ContentCreation",
            "#SocialMedia",
        ],
    ),
    (
        "education",
        &["#Education", "#Learning", "#Tutorial", "#HowTo"],
    ),
    (
        "finance",
        &["#Finance", "#Investing", "#MoneyTips", "#FinancialLiteracy"],
    ),
    (
        "health",
        &["#Health", "#Wellness", "#Fitness", "#HealthyLiving"],
    ),
    (
        "gaming",
        &["#Gaming", "#Gamer", "#Gameplay", "#GamingCommunity"],
    ),
    (
        "music",
        &["#Music", "#NewMusic", "#Musician", "#MusicVideo"],
    ),
    ("food", &["#Food", "#Cooking", "#Recipe", "#Foodie"]),
];

/// `IDEA_TEMPLATES`, in the shipped order.
const IDEA_TEMPLATES: &[&str] = &[
    "5 things nobody tells you about {topic}",
    "The {topic} mistake that costs you the most",
    "How I turned {topic} into a daily habit",
    "Behind the scenes: {topic} in 60 seconds",
    "What {topic} actually looks like in 2026",
    "The one {topic} tip that changed everything",
    "{topic} for beginners: start here",
    "Why most {topic} advice is wrong",
    "POV: you just discovered {topic}",
    "3 tools I use for {topic} every day",
    "The dark side of {topic}",
    "Can you master {topic} in 7 days?",
];

/// `IDEA_HOOKS`, in the shipped order.
const IDEA_HOOKS: &[&str] = &[
    "listicle",
    "mistake",
    "story",
    "behind-the-scenes",
    "trend",
    "tip",
    "tutorial",
    "hot-take",
    "pov",
    "tools",
    "controversy",
    "challenge",
];

/// `QUESTION_STARTERS` inside `generate_caption_deterministic`.
const QUESTION_STARTERS: &[&str] = &[
    "Did you know?",
    "Here's the thing:",
    "Wait for it...",
    "This changed everything:",
    "You need to see this:",
];

/// `PLATFORM_CHAR_LIMITS.get(platform, 2200)`.
fn char_limit(platform: &str) -> usize {
    PLATFORM_CHAR_LIMITS
        .iter()
        .find(|(name, _)| *name == platform)
        .map_or(2200, |(_, limit)| *limit)
}

/// Python's `text[:n]` — a slice by *characters*, not bytes.
fn take_chars(text: &str, count: usize) -> String {
    text.chars().take(count).collect()
}

/// Python's `len(text)` — code points.
fn char_len(text: &str) -> usize {
    text.chars().count()
}

/// `re.split(r"[.!?]+", text)`, then `strip()` and `len(...) > 10`.
fn sentences(text: &str) -> Vec<String> {
    let mut parts: Vec<String> = Vec::new();
    let mut current = String::new();
    for character in text.chars() {
        if matches!(character, '.' | '!' | '?') {
            parts.push(std::mem::take(&mut current));
        } else {
            current.push(character);
        }
    }
    parts.push(current);
    parts
        .into_iter()
        .map(|part| part.trim().to_string())
        .filter(|part| char_len(part) > 10)
        .collect()
}

/// `re.findall(r"\b[a-zA-Z]{4,}\b", text.lower())`, in order.
///
/// A match is a maximal run of ASCII letters, at least four long, whose
/// neighbours are not word characters (`[A-Za-z0-9_]`) — that is what `\b`
/// means on both sides of the run.
fn keywords(text: &str) -> Vec<String> {
    let lowered = text.to_lowercase();
    let chars: Vec<char> = lowered.chars().collect();
    // Python's `\b` is Unicode-aware: `ï` is a word character, so `ünïcode`
    // contributes no `code` keyword. An ASCII-only test invents one.
    let is_word = |character: char| character.is_alphanumeric() || character == '_';
    let mut found = Vec::new();
    let mut index = 0;
    while index < chars.len() {
        if !chars[index].is_ascii_alphabetic() {
            index += 1;
            continue;
        }
        let start = index;
        while index < chars.len() && chars[index].is_ascii_alphabetic() {
            index += 1;
        }
        let end = index;
        let before_ok = start == 0 || !is_word(chars[start - 1]);
        let after_ok = end == chars.len() || !is_word(chars[end]);
        if before_ok && after_ok && end - start >= 4 {
            found.push(chars[start..end].iter().collect());
        }
    }
    found
}

/// `str.capitalize()` for an already-lowercased ASCII word.
fn capitalize(word: &str) -> String {
    let mut chars = word.chars();
    match chars.next() {
        Some(first) => {
            let mut out = String::new();
            out.extend(first.to_uppercase());
            out.push_str(&chars.as_str().to_lowercase());
            out
        }
        None => String::new(),
    }
}

/// `generate_caption_deterministic()`.
fn caption_variants(text: &str, platform: &str) -> Vec<(String, String, Option<&'static str>)> {
    let sentences = sentences(text);
    if sentences.is_empty() {
        // The fallback dicts carry no `style` key at all — the shipped
        // generator returns `{"caption", "hashtags"}` here and the payload
        // omits the field rather than inventing one.
        return vec![
            (
                "Check out this video!".to_string(),
                "#video #content".to_string(),
                None,
            ),
            (
                "New content alert!".to_string(),
                "#new #content".to_string(),
                None,
            ),
            ("Watch this!".to_string(), "#watch #video".to_string(), None),
        ];
    }

    // `word_freq` is a dict, so ties keep first-occurrence order; Python's
    // `sorted(..., reverse=True)` is stable, which preserves that order.
    let mut word_freq: Vec<(String, usize)> = Vec::new();
    for word in keywords(text) {
        match word_freq.iter_mut().find(|(known, _)| *known == word) {
            Some((_, count)) => *count += 1,
            None => word_freq.push((word, 1)),
        }
    }
    let mut top_words = word_freq;
    // `sort_by_key` is stable, so equal counts keep first-occurrence order —
    // Python's `sorted(..., reverse=True)` is stable too.
    top_words.sort_by_key(|left| std::cmp::Reverse(left.1));
    top_words.truncate(10);

    let mut suggested: Vec<String> = Vec::new();
    for (word, _) in &top_words {
        match HASHTAG_SUGGESTIONS
            .iter()
            .find(|(keyword, _)| keyword == word)
        {
            Some((_, tags)) => suggested.extend(tags.iter().take(2).map(|tag| (*tag).to_string())),
            None => suggested.push(format!("#{}", capitalize(word))),
        }
    }

    let mut seen: Vec<String> = Vec::new();
    let mut unique: Vec<String> = Vec::new();
    for tag in suggested {
        let key = tag.to_lowercase();
        if !seen.contains(&key) {
            seen.push(key);
            unique.push(tag);
        }
    }
    unique.truncate(5);

    let limit = char_limit(platform);

    // Variant 1: first compelling sentence + hashtags.
    let mut first = take_chars(&sentences[0], limit.saturating_sub(200))
        .trim()
        .to_string();
    if sentences.len() > 1 && char_len(&first) < limit / 2 {
        first.push(' ');
        first.push_str(take_chars(&sentences[1], 100).trim());
    }
    let hashtags_one = unique.iter().take(3).cloned().collect::<Vec<_>>().join(" ");
    let mut caption_one = format!("{first}\n\n{hashtags_one}");
    if char_len(&caption_one) > limit {
        caption_one = take_chars(&caption_one, limit);
    }

    // Variant 2: question-based hook.
    let starter = QUESTION_STARTERS[sentences.len() % QUESTION_STARTERS.len()];
    let second = &sentences[std::cmp::min(1, sentences.len() - 1)];
    let caption_two = format!(
        "{starter} {}",
        take_chars(second, limit.saturating_sub(200)).trim()
    );
    let hashtags_two = unique.iter().take(4).cloned().collect::<Vec<_>>().join(" ");
    let mut caption_two_full = format!("{caption_two}\n\n{hashtags_two}");
    if char_len(&caption_two_full) > limit {
        caption_two_full = take_chars(&caption_two_full, limit);
    }

    // Variant 3: summary + call to action.
    let summary = take_chars(&sentences[sentences.len() - 1], limit / 3)
        .trim()
        .to_string();
    let all_hashtags = unique.join(" ");
    let mut caption_three = format!("{summary}\n\nFollow for more! {all_hashtags}");
    if char_len(&caption_three) > limit {
        caption_three = take_chars(&caption_three, limit);
    }

    vec![
        (caption_one, hashtags_one, Some("direct")),
        (caption_two_full, hashtags_two, Some("hook")),
        (caption_three, all_hashtags, Some("summary")),
    ]
}

/// `generate_ideas_deterministic()`.
fn idea_variants(topic: &str, count: usize) -> Vec<(String, String)> {
    let topic = {
        let trimmed = topic.trim();
        if trimmed.is_empty() {
            "content creation".to_string()
        } else {
            trimmed.to_string()
        }
    };
    let count = count.clamp(1, 10);
    (0..count)
        .map(|index| {
            (
                IDEA_TEMPLATES[index % IDEA_TEMPLATES.len()].replace("{topic}", &topic),
                IDEA_HOOKS[index % IDEA_HOOKS.len()].to_string(),
            )
        })
        .collect()
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

    #[test]
    fn sentences_split_on_runs_of_terminators() {
        assert_eq!(
            sentences("Routing is hard.. Concurrency tuning matters a lot!"),
            vec![
                "Routing is hard".to_string(),
                "Concurrency tuning matters a lot".to_string()
            ]
        );
    }

    #[test]
    fn keywords_need_a_word_boundary_on_both_sides() {
        // `abc123def` has no boundary between the letters and the digits.
        assert!(keywords("abc123def").is_empty());
        assert_eq!(
            keywords("routing, concurrency"),
            vec!["routing", "concurrency"]
        );
        // Three letters is below the `{4,}` floor.
        assert!(keywords("the cat sat").is_empty());
    }

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
    fn an_empty_transcript_falls_back_to_the_fixed_captions() {
        let variants = caption_variants("", "instagram");
        assert_eq!(variants[0].0, "Check out this video!");
        assert_eq!(variants[1].0, "New content alert!");
        assert_eq!(variants[2].0, "Watch this!");
    }

    #[test]
    fn ideas_rotate_templates_and_hooks() {
        let ideas = idea_variants("rust engines", 3);
        assert_eq!(ideas[0].0, "5 things nobody tells you about rust engines");
        assert_eq!(ideas[0].1, "listicle");
        assert_eq!(ideas[2].0, "How I turned rust engines into a daily habit");
        assert_eq!(ideas[2].1, "story");
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
