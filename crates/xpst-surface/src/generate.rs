//! The deterministic content generators: `generate_caption_deterministic` and
//! `generate_ideas_deterministic`.
//!
//! Parity target: `src/xpst/caption_gen.py` (`PLATFORM_CHAR_LIMITS`,
//! `HASHTAG_SUGGESTIONS`, `IDEA_TEMPLATES`, `IDEA_HOOKS`, and the two
//! deterministic functions). Both are pure string algorithms: no config, no
//! store, no network.
//!
//! Two surfaces serve these: `xpst generate caption` / `xpst generate ideas`
//! (the CLI) and `xpst_suggest_caption` / `xpst_generate_ideas` (MCP). The
//! algorithms therefore live here, below both, and each surface only renders.
//!
//! **The LLM path is unreachable over MCP, and that is the shipped behaviour.**
//! `caption_gen.generate_caption` is `asyncio.run(generate_caption_llm(...))`
//! wrapped in `except RuntimeError`. The MCP server dispatches tool handlers
//! from inside a running event loop, so `asyncio.run` raises
//! `RuntimeError: asyncio.run() cannot be called from a running event loop`
//! and the handler always lands on the deterministic branch — even on a
//! machine with `XPST_KB_LLM_ENABLED` and a base URL configured. The CLI is a
//! separate process with no loop, so there the LLM path *is* reachable and the
//! CLI's divergence note stands. Two surfaces, two different contracts, and
//! the MCP one is fully portable offline.
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
//!
//! [`caption_seed_from_path`] is the other half of the CLI's contract: when no
//! transcript is available, `suggest-caption` seeds the generator with the
//! video's filename stem. It lives here with the generator because it is the
//! same `caption_gen` port; the MCP tool does not use it (it passes its
//! `video_path` argument through as the transcript text).

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

/// One caption variant: `(caption, hashtags, style)`.
///
/// `style` is `None` for the empty-transcript fallback, whose dicts carry no
/// `style` key at all — the payload omits the field rather than inventing one.
pub type CaptionVariant = (String, String, Option<&'static str>);

/// One idea: `(idea, hook)`.
pub type IdeaVariant = (String, String);

/// `PLATFORM_CHAR_LIMITS.get(platform, 2200)`.
pub fn char_limit(platform: &str) -> usize {
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
pub fn caption_variants(text: &str, platform: &str) -> Vec<CaptionVariant> {
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
///
/// `count` is clamped to 1..=10 here, exactly as the Python does — the MCP
/// schema's `minimum`/`maximum` are advisory, and the handler passes the raw
/// argument through.
pub fn idea_variants(topic: &str, count: usize) -> Vec<IdeaVariant> {
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

/// `Path(video_path).stem.replace("_", " ").replace("-", " ")` — the caption
/// seed `xpst suggest-caption` falls back to when no transcript is available.
///
/// `Path.stem` is **not** `Path::file_stem`, and the difference is observable:
///
/// * Python's `_splitext` keeps a trailing dot (`clip.` → `clip.`) and keeps a
///   leading-dot name whole (`.mp4` → `.mp4`); `file_stem` strips both.
/// * `PurePath.name` answers `""` for `.` and for a root, but `..` for `..`;
///   `file_stem` answers `None` for all three.
/// * On Windows `PureWindowsPath` treats `\` as a separator too and splits a
///   drive off the first component, so `C:clip.mp4` has the name `clip.mp4`
///   and `C:` has the name `""`.
///
/// The grid in `tests/fixtures/python_caption_seed.json` is captured from the
/// shipped interpreter (both flavours) and replayed by `tests/caption_seed.rs`.
pub fn caption_seed_from_path(video_path: &str) -> String {
    caption_seed_from_path_with(video_path, cfg!(windows))
}

/// [`caption_seed_from_path`] with the path flavour injected.
///
/// The shipped entry point uses the host's flavour; the parity test drives both
/// so the Windows rows are pinned on any host instead of waiting for a runner.
pub fn caption_seed_from_path_with(video_path: &str, windows: bool) -> String {
    // Python applies the two replacements in sequence; both map to a space, so
    // one pass over the pair is the same string (and clippy's preferred form).
    python_stem(&python_name(video_path, windows)).replace(['_', '-'], " ")
}

/// `PurePath.name` — the final component, with empty and `.` components
/// dropped (`a/.` is `a`, `.` is `""`, `..` is `..`).
fn python_name(path: &str, windows: bool) -> String {
    let separators: &[char] = if windows { &['/', '\\'] } else { &['/'] };
    let mut components: Vec<&str> = path
        .split(|character| separators.contains(&character))
        .filter(|component| !component.is_empty() && *component != ".")
        .collect();
    if windows {
        // A drive is its own component even without a separator:
        // `C:clip.mp4` → `("C:", "clip.mp4")`, `C:` → `("C:",)` → name `""`.
        if let Some(rest) = components.first().copied().and_then(split_drive) {
            components.remove(0);
            if !rest.is_empty() {
                components.insert(0, rest);
            }
        }
    }
    components.last().copied().unwrap_or_default().to_string()
}

/// `ntpath.splitdrive`'s drive half: `C:rest` → `rest`, anything else → `None`.
fn split_drive(component: &str) -> Option<&str> {
    let bytes = component.as_bytes();
    if bytes.len() >= 2 && bytes[0].is_ascii_alphabetic() && bytes[1] == b':' {
        Some(&component[2..])
    } else {
        None
    }
}

/// `PurePath.stem` — `_splitext` keeps the name whole unless the last dot has
/// something on both sides of it.
fn python_stem(name: &str) -> String {
    let characters: Vec<char> = name.chars().collect();
    match characters.iter().rposition(|character| *character == '.') {
        Some(index) if index > 0 && index + 1 < characters.len() => {
            characters[..index].iter().collect()
        }
        _ => name.to_string(),
    }
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
        // `abc123def` is one word-character run, so neither half has a
        // boundary on both sides; `ünïcode` contributes no `code`.
        assert_eq!(keywords("abc123def ünïcode parity"), vec!["parity"]);
    }

    #[test]
    fn an_empty_transcript_falls_back_to_the_fixed_captions() {
        let variants = caption_variants("", "instagram");
        assert_eq!(variants.len(), 3);
        assert_eq!(variants[0].0, "Check out this video!");
        assert_eq!(variants[0].2, None, "the fallback carries no style key");
    }

    #[test]
    fn ideas_rotate_templates_and_hooks() {
        let ideas = idea_variants("parity topic", 3);
        assert_eq!(ideas.len(), 3);
        assert_eq!(ideas[0].0, "5 things nobody tells you about parity topic");
        assert_eq!(ideas[0].1, "listicle");
        assert_eq!(ideas[2].1, "story");
    }

    #[test]
    fn a_blank_topic_uses_the_default_but_the_count_still_clamps() {
        assert_eq!(
            idea_variants("   ", 1)[0].0,
            "5 things nobody tells you about content creation"
        );
        assert_eq!(idea_variants("x", 99).len(), 10);
        assert_eq!(idea_variants("x", 0).len(), 1);
    }

    #[test]
    fn the_char_limit_table_is_the_shipped_one() {
        assert_eq!(char_limit("x"), 280);
        assert_eq!(char_limit("threads"), 500);
        assert_eq!(char_limit("youtube"), 5000);
        assert_eq!(char_limit("nope"), 2200);
    }

    /// The four shapes `Path::file_stem` gets wrong. The full grid is replayed
    /// from the captured fixture in `tests/caption_seed.rs`; these are the
    /// cases a reader needs to see named.
    #[test]
    fn the_seed_is_pythons_stem_not_rusts_file_stem() {
        assert_eq!(caption_seed_from_path_with("clip.", false), "clip.");
        assert_eq!(caption_seed_from_path_with(".mp4", false), ".mp4");
        assert_eq!(caption_seed_from_path_with("..", false), "..");
        assert_eq!(caption_seed_from_path_with(".", false), "");
        assert_eq!(caption_seed_from_path_with("/", false), "");
        assert_eq!(caption_seed_from_path_with("a/.", false), "a");
        assert_eq!(caption_seed_from_path_with("clip..mp4", false), "clip.");
    }

    #[test]
    fn the_seed_replaces_underscores_and_hyphens_with_spaces() {
        assert_eq!(
            caption_seed_from_path_with("my_clip-final.mp4", false),
            "my clip final"
        );
        assert_eq!(
            caption_seed_from_path_with("under_score-and-dash.mp4", false),
            "under score and dash"
        );
        // A leading dash becomes a leading space; Python does not trim it.
        assert_eq!(
            caption_seed_from_path_with("-leading-dash.mp4", false),
            " leading dash"
        );
    }

    #[test]
    fn the_windows_flavour_splits_backslashes_and_drives() {
        assert_eq!(
            caption_seed_from_path_with("C:\\clips\\my_clip.mp4", true),
            "my clip"
        );
        assert_eq!(caption_seed_from_path_with("C:clip.mp4", true), "clip");
        assert_eq!(caption_seed_from_path_with("C:", true), "");
        assert_eq!(
            caption_seed_from_path_with("\\\\server\\share\\clip.mp4", true),
            "clip"
        );
        // The POSIX flavour keeps the backslash, so the whole string is one
        // component — that is what `PurePosixPath` does, and the fixture pins
        // it rather than pretending the two agree.
        assert_eq!(
            caption_seed_from_path_with("C:\\clips\\my_clip.mp4", false),
            "C:\\clips\\my clip"
        );
    }
}
