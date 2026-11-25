//! A PyYAML-compatible block-style emitter.
//!
//! `xpst config export`, `config set`, `config import` and `config fix` all
//! write their file with
//!
//! ```text
//! yaml.dump(document, f, default_flow_style=False, sort_keys=False)
//! ```
//!
//! so the Rust engine needs a writer that produces the same bytes, not merely
//! valid YAML: the exported file is something a user reads, diffs and commits,
//! and "the two engines export different files" is a parity defect even when
//! both re-parse to the same document.
//!
//! `serde-saphyr`'s serializer (what [`crate::yaml::to_string`] uses) is
//! deliberately *not* byte-identical — it double-quotes scalars a YAML 1.1
//! reader could mistype and folds differently. That is fine for `config show`,
//! which prints a document for a human, and wrong for a file the Python engine
//! also writes. This module reproduces PyYAML's decisions instead:
//!
//! * block style, two-space indent, sequences indented at their parent's level
//!   (PyYAML's `indentless` block sequence);
//! * plain scalars only when the YAML 1.1 resolver would read them back as the
//!   same string, single quotes otherwise, double quotes when the text carries
//!   a tab, a control character or a non-ASCII character (`allow_unicode` is
//!   off by default, so `café` becomes `"caf\xE9"`);
//! * line folding at column 80 for plain and quoted scalars, breaking at a
//!   single space and continuing at the scalar's indent.
//!
//! The behaviour is pinned by `tests/pyyaml_parity.rs` against
//! `tests/fixtures/pyyaml_corpus.json`, which
//! `xtask/scripts/capture_pyyaml_corpus.py` generates by running the real
//! PyYAML. Re-run that script to refresh the fixture; never hand-edit it.

use crate::yaml::Value;

/// PyYAML's default `width`.
const BEST_WIDTH: usize = 80;
/// PyYAML's default `indent`.
const INDENT: usize = 2;

/// Render a document exactly as `yaml.dump(value, default_flow_style=False,
/// sort_keys=False)` does.
pub fn dump(value: &Value) -> String {
    let mut emitter = Emitter::default();
    match value {
        Value::Mapping(map) if !map.is_empty() => emitter.block(value, 0, false),
        Value::Sequence(items) if !items.is_empty() => emitter.block(value, 0, false),
        // A root that is a scalar or an empty collection is written inline; a
        // scalar root also gets PyYAML's `...` end marker, because a bare
        // scalar document is otherwise ambiguous with a directive.
        Value::Mapping(_) => emitter.write("{}"),
        Value::Sequence(_) => emitter.write("[]"),
        other => {
            // A root scalar written in plain style is `open_ended` in PyYAML,
            // so the document gets an explicit `...` end marker; a quoted one
            // is unambiguous and does not.
            let plain = match other {
                Value::String(text) => analyze(text).allow_plain && !resolves_to_non_string(text),
                _ => true,
            };
            // `expect_scalar` increases the indent before writing, so a root
            // scalar folds at column 2, not 0.
            emitter.scalar(other, INDENT);
            if plain {
                emitter.write("\n...");
            }
        }
    }
    emitter.out.push('\n');
    emitter.out
}

struct Emitter {
    out: String,
    column: usize,
    /// True while nothing has been written on the current line, so the first
    /// `write_indent` of a document does not open with a blank line.
    line_start: bool,
}

impl Default for Emitter {
    fn default() -> Self {
        Self {
            out: String::new(),
            column: 0,
            line_start: true,
        }
    }
}

impl Emitter {
    fn write(&mut self, text: &str) {
        self.out.push_str(text);
        self.column += text.chars().count();
        if !text.is_empty() {
            self.line_start = false;
        }
    }

    /// `write_indent()`: a line break plus the current indent.
    fn write_indent(&mut self, indent: usize) {
        if !self.line_start {
            self.out.push('\n');
        }
        for _ in 0..indent {
            self.out.push(' ');
        }
        self.column = indent;
        self.line_start = true;
    }

    /// Emit a block collection at `indent`.
    ///
    /// `inline_first` is set when the caller has already written the `- ` of a
    /// compact sequence entry, so the first line must not be indented again.
    fn block(&mut self, value: &Value, indent: usize, inline_first: bool) {
        match value {
            Value::Mapping(map) => {
                for (index, (key, item)) in map.iter().enumerate() {
                    if !(index == 0 && inline_first) {
                        self.write_indent(indent);
                    }
                    self.key(key, indent);
                    self.write(":");
                    self.value_after_key(item, indent);
                }
            }
            Value::Sequence(items) => {
                for (index, item) in items.iter().enumerate() {
                    if !(index == 0 && inline_first) {
                        self.write_indent(indent);
                    }
                    self.write("-");
                    match item {
                        Value::Mapping(map) if !map.is_empty() => {
                            self.write(" ");
                            self.block(item, indent + INDENT, true);
                        }
                        Value::Sequence(inner) if !inner.is_empty() => {
                            self.write(" ");
                            self.block(item, indent + INDENT, true);
                        }
                        other => {
                            self.write(" ");
                            self.scalar(other, indent + INDENT);
                        }
                    }
                }
            }
            other => self.scalar(other, indent),
        }
    }

    /// A mapping key. PyYAML writes a simple key on one line; a collection key
    /// uses the explicit `? ` form.
    fn key(&mut self, key: &Value, indent: usize) {
        match key {
            Value::Mapping(map) if !map.is_empty() => {
                self.write("? ");
                self.block(key, indent + INDENT, true);
                self.write_indent(indent);
            }
            Value::Sequence(items) if !items.is_empty() => {
                self.write("? ");
                self.block(key, indent + INDENT, true);
                self.write_indent(indent);
            }
            other => self.scalar(other, indent),
        }
    }

    /// The value half of `key: value`, including the separator.
    fn value_after_key(&mut self, value: &Value, indent: usize) {
        match value {
            Value::Mapping(map) if !map.is_empty() => {
                self.block(value, indent + INDENT, false);
            }
            // PyYAML's block sequence is *indentless*: it sits at the parent
            // mapping's indent, not one level in.
            Value::Sequence(items) if !items.is_empty() => {
                self.block(value, indent, false);
            }
            other => {
                self.write(" ");
                self.scalar(other, indent + INDENT);
            }
        }
    }

    /// A scalar (or an empty collection) in block context.
    fn scalar(&mut self, value: &Value, indent: usize) {
        match value {
            Value::Null => self.write("null"),
            Value::Bool(true) => self.write("true"),
            Value::Bool(false) => self.write("false"),
            Value::Number(number) => {
                let text = render_number(number);
                self.write(&text);
            }
            // PyYAML writes these back in their own spelling, unquoted.
            Value::NonFinite(kind) => self.write(kind.as_yaml()),
            Value::Timestamp(text) => self.write(text),
            Value::String(text) => self.string(text, indent),
            Value::Mapping(_) => self.write("{}"),
            Value::Sequence(_) => self.write("[]"),
        }
    }

    fn string(&mut self, text: &str, indent: usize) {
        let analysis = analyze(text);
        if analysis.allow_plain && !resolves_to_non_string(text) {
            self.write_plain(text, indent);
        } else if analysis.allow_single {
            self.write_single_quoted(text, indent);
        } else {
            self.write_double_quoted(text, indent);
        }
    }

    /// `write_plain()`: fold at a single space once the column passes the
    /// width, exactly as PyYAML does.
    fn write_plain(&mut self, text: &str, indent: usize) {
        if text.is_empty() {
            return;
        }
        let chars: Vec<char> = text.chars().collect();
        let mut spaces = false;
        let mut start = 0usize;
        let mut end = 0usize;
        while end <= chars.len() {
            let ch = chars.get(end).copied();
            if spaces {
                if ch != Some(' ') {
                    if start + 1 == end && self.column > BEST_WIDTH {
                        self.write_indent(indent);
                    } else {
                        let data: String = chars[start..end].iter().collect();
                        self.write(&data);
                    }
                    start = end;
                }
            } else if ch.is_none() || ch == Some(' ') {
                let data: String = chars[start..end].iter().collect();
                self.write(&data);
                start = end;
            }
            if let Some(ch) = ch {
                spaces = ch == ' ';
            }
            end += 1;
        }
    }

    /// `write_single_quoted()`: `'` is doubled, and the same fold rule applies.
    fn write_single_quoted(&mut self, text: &str, indent: usize) {
        self.write("'");
        let chars: Vec<char> = text.chars().collect();
        let mut spaces = false;
        let mut breaks = false;
        let mut start = 0usize;
        let mut end = 0usize;
        while end <= chars.len() {
            let ch = chars.get(end).copied();
            if spaces {
                if ch != Some(' ') {
                    if start + 1 == end
                        && self.column > BEST_WIDTH
                        && start != 0
                        && end != chars.len()
                    {
                        self.write_indent(indent);
                    } else {
                        let data: String = chars[start..end].iter().collect();
                        self.write(&data);
                    }
                    start = end;
                }
            } else if breaks {
                if ch != Some('\n') {
                    // A run of line breaks is written as blank lines, then the
                    // continuation is indented.
                    // PyYAML writes one break for the run's leading newline
                    // plus one per newline in the run, then indents the
                    // continuation — so `a\nb` becomes `'a\n\n  b'`.
                    let run: String = chars[start..end].iter().collect();
                    for _ in 0..=run.chars().filter(|c| *c == '\n').count() {
                        self.out.push('\n');
                    }
                    self.column = 0;
                    self.line_start = true;
                    self.write_indent(indent);
                    start = end;
                }
            } else if (ch.is_none() || ch == Some(' ') || ch == Some('\n') || ch == Some('\''))
                && start < end
            {
                let data: String = chars[start..end].iter().collect();
                self.write(&data);
                start = end;
            }
            if ch == Some('\'') {
                self.write("''");
                start = end + 1;
            }
            if let Some(ch) = ch {
                spaces = ch == ' ';
                breaks = ch == '\n';
            }
            end += 1;
        }
        self.write("'");
    }

    /// `write_double_quoted()`: escapes everything `allow_unicode=False` cannot
    /// carry, and folds at a single space like the other styles.
    fn write_double_quoted(&mut self, text: &str, indent: usize) {
        self.write("\"");
        let chars: Vec<char> = text.chars().collect();
        let mut spaces = false;
        let mut breaks = false;
        let mut start = 0usize;
        let mut end = 0usize;
        while end <= chars.len() {
            let ch = chars.get(end).copied();
            if spaces {
                if ch != Some(' ') {
                    if start + 1 == end
                        && self.column > BEST_WIDTH
                        && start != 0
                        && end != chars.len()
                    {
                        self.write_indent(indent);
                    } else {
                        let data: String = chars[start..end].iter().collect();
                        self.write(&data);
                    }
                    start = end;
                }
            } else if breaks {
                if ch != Some('\n') {
                    // PyYAML writes one break for the run's leading newline
                    // plus one per newline in the run, then indents the
                    // continuation — so `a\nb` becomes `'a\n\n  b'`.
                    let run: String = chars[start..end].iter().collect();
                    for _ in 0..=run.chars().filter(|c| *c == '\n').count() {
                        self.out.push('\n');
                    }
                    self.column = 0;
                    self.line_start = true;
                    self.write_indent(indent);
                    start = end;
                }
            } else if (ch.is_none()
                || ch == Some(' ')
                || ch == Some('\n')
                || ch.is_some_and(needs_escape))
                && start < end
            {
                let data: String = chars[start..end].iter().collect();
                self.write(&data);
                start = end;
            }
            if let Some(ch) = ch {
                if let Some(escaped) = escape(ch) {
                    self.write(&escaped);
                    start = end + 1;
                }
                spaces = ch == ' ';
                breaks = ch == '\n';
            }
            end += 1;
        }
        self.write("\"");
    }
}

/// Characters PyYAML escapes in a double-quoted scalar with `allow_unicode`
/// off.
fn needs_escape(ch: char) -> bool {
    escape(ch).is_some()
}

fn escape(ch: char) -> Option<String> {
    let named = match ch {
        '\0' => Some("\\0"),
        '\u{7}' => Some("\\a"),
        '\u{8}' => Some("\\b"),
        '\t' => Some("\\t"),
        '\n' => Some("\\n"),
        '\u{b}' => Some("\\v"),
        '\u{c}' => Some("\\f"),
        '\r' => Some("\\r"),
        '\u{1b}' => Some("\\e"),
        '"' => Some("\\\""),
        '\\' => Some("\\\\"),
        '\u{85}' => Some("\\N"),
        '\u{a0}' => Some("\\_"),
        '\u{2028}' => Some("\\L"),
        '\u{2029}' => Some("\\P"),
        _ => None,
    };
    if let Some(named) = named {
        return Some(named.to_string());
    }
    let code = ch as u32;
    if (0x20..=0x7e).contains(&code) {
        return None;
    }
    Some(if code <= 0xff {
        format!("\\x{code:02X}")
    } else if code <= 0xffff {
        format!("\\u{code:04X}")
    } else {
        format!("\\U{code:08X}")
    })
}

/// Python's `repr(float)`, which is what PyYAML writes for a float.
///
/// Rust's `Display` is shortest-round-trip too, but it never switches to
/// exponent form, so `1e20` would print as 21 digits and `1e-7` as
/// `0.0000001`. Python switches when the decimal exponent leaves `-4..=15` and
/// always keeps a `.0` mantissa and a two-digit exponent.
fn render_number(number: &crate::yaml::Number) -> String {
    if let Some(integer) = number.as_i64() {
        return integer.to_string();
    }
    if let Some(integer) = number.as_u64() {
        return integer.to_string();
    }
    let value = number.as_f64().unwrap_or_default();
    render_float(value)
}

/// Python's `repr(float)` — the same rendering PyYAML writes, exposed because
/// `config set` prints the parsed value with `str()`.
pub fn python_float_repr(value: f64) -> String {
    render_float(value)
}

/// Python's `repr(float)` without PyYAML's `.0` insertion.
///
/// `yaml.dump` writes `1.0e+20` (it inserts `.0` so the scalar still reads as a
/// float), while `json.dumps` writes `1e+20`. Both are `repr()` plus a tweak, so
/// they share the digit logic here.
pub fn python_json_float(value: f64) -> String {
    render_float_with(value, false)
}

fn render_float(value: f64) -> String {
    render_float_with(value, true)
}

fn render_float_with(value: f64, yaml_style: bool) -> String {
    if value.is_nan() {
        return ".nan".to_string();
    }
    if value.is_infinite() {
        return if value > 0.0 { ".inf" } else { "-.inf" }.to_string();
    }
    // `{:e}` is shortest-round-trip in scientific form: `1.5e0`, `-5e-1`.
    let scientific = format!("{value:e}");
    let (mantissa, exponent) = scientific
        .split_once('e')
        .map(|(mantissa, exponent)| (mantissa, exponent.parse::<i32>().unwrap_or(0)))
        .unwrap_or((scientific.as_str(), 0));
    let negative = mantissa.starts_with('-');
    let digits: String = mantissa
        .trim_start_matches('-')
        .chars()
        .filter(|ch| *ch != '.')
        .collect();
    let sign = if negative { "-" } else { "" };

    if (-4..16).contains(&exponent) {
        let mut text = String::new();
        text.push_str(sign);
        if exponent >= 0 {
            let point = exponent as usize + 1;
            if digits.len() <= point {
                text.push_str(&digits);
                text.push_str(&"0".repeat(point - digits.len()));
                text.push_str(".0");
            } else {
                text.push_str(&digits[..point]);
                text.push('.');
                text.push_str(&digits[point..]);
            }
        } else {
            text.push_str("0.");
            text.push_str(&"0".repeat((-exponent - 1) as usize));
            text.push_str(&digits);
        }
        return text;
    }

    let mut text = String::new();
    text.push_str(sign);
    text.push_str(&digits[..1]);
    if digits.len() > 1 {
        text.push('.');
        text.push_str(&digits[1..]);
    } else if yaml_style {
        text.push_str(".0");
    }
    text.push('e');
    text.push(if exponent < 0 { '-' } else { '+' });
    text.push_str(&format!("{:02}", exponent.abs()));
    text
}

/// The flags PyYAML's `analyze_scalar()` derives.
struct Analysis {
    allow_plain: bool,
    allow_single: bool,
}

fn analyze(text: &str) -> Analysis {
    if text.is_empty() {
        return Analysis {
            allow_plain: true,
            allow_single: true,
        };
    }
    let chars: Vec<char> = text.chars().collect();
    let mut block_indicators = false;
    let mut line_breaks = false;
    let mut special_characters = false;
    let mut leading_space = false;
    let mut trailing_space = false;
    let mut break_space = false;
    let mut space_break = false;

    if text.starts_with("---") || text.starts_with("...") {
        block_indicators = true;
    }

    let mut preceded_by_whitespace = true;
    for (index, ch) in chars.iter().copied().enumerate() {
        let followed_by_whitespace = index + 1 >= chars.len()
            || matches!(chars[index + 1], ' ' | '\t' | '\r' | '\n' | '\u{85}');
        if index == 0 {
            if matches!(
                ch,
                '#' | ','
                    | '['
                    | ']'
                    | '{'
                    | '}'
                    | '&'
                    | '*'
                    | '!'
                    | '|'
                    | '>'
                    | '\''
                    | '"'
                    | '%'
                    | '@'
                    | '`'
            ) {
                block_indicators = true;
            }
            if matches!(ch, '?' | ':') && followed_by_whitespace {
                block_indicators = true;
            }
            if ch == '-' && followed_by_whitespace {
                block_indicators = true;
            }
        } else {
            if ch == ':' && followed_by_whitespace {
                block_indicators = true;
            }
            if ch == '#' && preceded_by_whitespace {
                block_indicators = true;
            }
        }
        if matches!(ch, '\n' | '\u{85}' | '\u{2028}' | '\u{2029}') {
            line_breaks = true;
        }
        if !(ch == '\n' || ('\u{20}'..='\u{7e}').contains(&ch)) {
            special_characters = true;
        }
        preceded_by_whitespace = matches!(ch, ' ' | '\t' | '\r' | '\n' | '\u{85}');
    }

    if matches!(chars[0], ' ' | '\t' | '\r' | '\n' | '\u{85}') {
        leading_space = true;
    }
    if matches!(chars[chars.len() - 1], ' ' | '\t' | '\r' | '\n' | '\u{85}') {
        trailing_space = true;
    }
    for window in chars.windows(2) {
        if matches!(window[0], '\n' | '\u{85}') && matches!(window[1], ' ' | '\t') {
            break_space = true;
        }
        if matches!(window[0], ' ' | '\t') && matches!(window[1], '\n' | '\u{85}') {
            space_break = true;
        }
    }

    // PyYAML's rules, verbatim: a line break alone does *not* rule out single
    // quotes (a multiline string is written single-quoted with literal breaks);
    // only a space adjacent to a break, or a special character, does.
    Analysis {
        allow_plain: !(leading_space
            || trailing_space
            || break_space
            || space_break
            || special_characters
            || line_breaks
            || block_indicators),
        allow_single: !(break_space || space_break || special_characters),
    }
}

/// True when a YAML 1.1 reader would resolve this plain scalar to something
/// other than a string, which is why PyYAML quotes it.
///
/// The resolver is shared with the reader ([`crate::schema`]) so the two
/// directions cannot drift: a string the writer leaves plain is exactly a
/// string the reader will read back as a string.
fn resolves_to_non_string(text: &str) -> bool {
    crate::schema::resolve_plain(text).is_some()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dump_str(text: &str) -> String {
        dump(&Value::String(text.to_string()))
    }

    #[test]
    fn plain_scalars_stay_plain() {
        assert_eq!(dump_str("plain"), "plain\n...\n");
        assert_eq!(dump_str("with space"), "with space\n...\n");
        assert_eq!(dump_str("a,b"), "a,b\n...\n");
        assert_eq!(dump_str("it's"), "it's\n...\n");
    }

    #[test]
    fn ambiguous_scalars_are_single_quoted() {
        // A quoted root scalar is unambiguous, so PyYAML adds no `...` marker.
        assert_eq!(dump_str("10"), "'10'\n");
        assert_eq!(dump_str("true"), "'true'\n");
        assert_eq!(dump_str("null"), "'null'\n");
        assert_eq!(dump_str("2026-01-01"), "'2026-01-01'\n");
        assert_eq!(dump_str("a: b"), "'a: b'\n");
        assert_eq!(dump_str(""), "''\n");
    }

    #[test]
    fn non_ascii_and_control_characters_are_double_quoted() {
        assert_eq!(dump_str("café"), "\"caf\\xE9\"\n");
        assert_eq!(dump_str("tab\there"), "\"tab\\there\"\n");
    }

    #[test]
    fn floats_render_like_python_repr() {
        assert_eq!(render_float(1.5), "1.5");
        assert_eq!(render_float(2.0), "2.0");
        assert_eq!(render_float(-0.5), "-0.5");
        assert_eq!(render_float(1e20), "1.0e+20");
        assert_eq!(render_float(1e-7), "1.0e-07");
        assert_eq!(render_float(1e15), "1000000000000000.0");
    }

    #[test]
    fn a_mapping_uses_block_style_with_indentless_sequences() {
        let mut map = crate::yaml::Mapping::new();
        map.insert(Value::from("list"), Value::Sequence(vec![Value::from("a")]));
        let mut inner = crate::yaml::Mapping::new();
        inner.insert(Value::from("k"), Value::from(1i64));
        map.insert(Value::from("nested"), Value::Mapping(inner));
        assert_eq!(dump(&Value::Mapping(map)), "list:\n- a\nnested:\n  k: 1\n");
    }
}
