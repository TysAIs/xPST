//! Rich-compatible rendering for the config-error path.
//!
//! The Python CLI reports a config failure with `console.print(...)` on
//! **stdout** and logs the corruption notice through a `RichHandler` on
//! **stderr**. Both are Rich renderings, not plain `print()` calls: the text is
//! word-wrapped at the console width, long words are folded, and the log line
//! is laid out as a time/level/message table. A Rust engine that printed the
//! same sentence unwrapped would still be a different answer, so the two
//! algorithms are ported here rather than approximated:
//!
//! * [`wrap`] is `rich.text.Text.wrap` — `divide_line` + `chop_cells` +
//!   `rstrip_end` + `truncate`, with Rich's cell widths (a CJK character is two
//!   cells, a combining mark is zero);
//! * [`log_error`] is `rich._log_render.LogRender` with `show_time`,
//!   `show_level` and no path column, which is what `RichHandler` builds.
//!
//! **Named divergences.** Python asks the terminal for its width when any of
//! stdin/stdout/stderr is a TTY (`Console.size`), and falls back to `COLUMNS`
//! and then 80. This workspace forbids `unsafe_code`, so the ioctl that would
//! answer that question is not available here; the engine honours `COLUMNS` and
//! otherwise assumes 80. The parity harness pins `COLUMNS=80`, so both engines
//! agree there; a TTY user gets 80 columns instead of their terminal's width.
//!
//! At `COLUMNS` ≤ 30 the stderr block also diverges: Rich shrinks the time and
//! level columns (the stamp truncates to `[11/23/26`) and gives the message the
//! remainder, while [`log_error`] keeps the full 29-cell prefix and lets the
//! row overflow the width. Reimplementing Rich's column shrink is not worth it
//! for a width no real terminal uses; the ledger records it. stdout still
//! matches at every width.
//!
//! The log timestamp is likewise the C-locale rendering of Python's `%x %X`
//! (`[%m/%d/%y %H:%M:%S]`), not a locale-dependent one.

use std::io::{self, Write};

/// The console width Rich would use: `COLUMNS` when it is a positive integer,
/// otherwise 80.
///
/// Python's rule is `columns.isdigit()` — no surrounding whitespace, no sign —
/// and a parsed `0` falls through to 80 (`width or 80`).
#[must_use]
pub fn console_width() -> usize {
    let columns = std::env::var("COLUMNS").ok();
    let parsed = columns
        .as_deref()
        .filter(|value| !value.is_empty() && value.chars().all(|ch| ch.is_ascii_digit()))
        .and_then(|value| value.parse::<usize>().ok())
        .filter(|width| *width > 0);
    parsed.unwrap_or(80)
}

/// Wrap `text` the way `rich.text.Text.wrap` does at `width` cells.
///
/// Blank lines are preserved (`split(allow_blank=True)`), tabs are expanded to
/// 8-column stops, and a word longer than the line is folded rather than
/// cropped — which is why a long config path appears in the middle of a word.
#[must_use]
pub fn wrap(text: &str, width: usize) -> Vec<String> {
    let width = width.max(1);
    let mut lines = Vec::new();
    for raw in text.split('\n') {
        let mut chars: Vec<char> = raw.chars().collect();
        if chars.contains(&'\t') {
            expand_tabs(&mut chars, 8);
        }
        let breaks = divide_line(&chars, width, true);
        let mut start = 0;
        for offset in breaks {
            lines.push(render_line(&chars[start..offset], width));
            start = offset;
        }
        lines.push(render_line(&chars[start..], width));
    }
    lines
}

/// `console.print(f"[red]{prefix}[/red] {message}")`: plain text on stdout.
///
/// The markup only colours the prefix; with no colour system (a pipe) the
/// rendered text is the prefix and the message, wrapped at the console width.
pub fn print_error(out: &mut dyn Write, prefix: &str, message: &str) -> io::Result<()> {
    let text = format!("{prefix}{message}");
    for line in wrap(&text, console_width()) {
        writeln!(out, "{line}")?;
    }
    Ok(())
}

/// The `RichHandler` block for one `ERROR` record, on stderr.
///
/// Layout: `[time]` (19 cells), one space, the level padded to 8, one space,
/// then the message wrapped at the remaining width and padded to it — the
/// `Table.grid(padding=(0, 1))` that `LogRender` builds with `expand = True`.
pub fn log_error(err: &mut dyn Write, message: &str) -> io::Result<()> {
    let width = console_width();
    let stamp = format!(
        "[{}]",
        jiff::ZonedDateTime::now().strftime("%m/%d/%y %H:%M:%S")
    );
    let prefix = format!("{stamp} {:<8} ", "ERROR");
    let prefix_cells = cell_len(&prefix);
    let message_width = width.saturating_sub(prefix_cells).max(1);
    // The time and level are cells of a single table row, so they appear on the
    // first line only; the wrapped message continues under a blank gutter.
    let gutter = " ".repeat(prefix_cells);
    for (index, line) in wrap(message, message_width).into_iter().enumerate() {
        let lead = if index == 0 { &prefix } else { &gutter };
        let mut row = format!("{lead}{line}");
        let cells = cell_len(&row);
        if cells < width {
            row.push_str(&" ".repeat(width - cells));
        }
        writeln!(err, "{row}")?;
    }
    Ok(())
}

/// `Text.expand_tabs`: advance to the next multiple of `tab_size`.
///
/// Rich counts tab stops in *cells* (`cell_position += part.cell_len`), not in
/// characters, so a wide character before a tab advances the stop by two.
fn expand_tabs(chars: &mut Vec<char>, tab_size: usize) {
    let mut column = 0;
    let mut expanded = Vec::with_capacity(chars.len());
    for ch in chars.iter() {
        if *ch == '\t' {
            let advance = tab_size - (column % tab_size);
            expanded.extend(std::iter::repeat_n(' ', advance));
            column += advance;
        } else {
            expanded.push(*ch);
            column += cell_width(*ch);
        }
    }
    *chars = expanded;
}

/// `divide_line`: the cell offsets a line should be split at.
fn divide_line(chars: &[char], width: usize, fold: bool) -> Vec<usize> {
    let mut breaks: Vec<usize> = Vec::new();
    let mut cell_offset = 0usize;

    for (start, word) in words(chars) {
        let word_length = cell_len_chars(trim_end_whitespace(word));
        let fits = cell_offset + word_length <= width;

        if fits {
            cell_offset += cell_len_chars(word);
        } else if word_length > width {
            if fold {
                let folded = chop_cells(word, width);
                let last_index = folded.len().saturating_sub(1);
                let mut offset = start;
                for (index, line) in folded.iter().enumerate() {
                    if offset > 0 {
                        breaks.push(offset);
                    }
                    if index == last_index {
                        cell_offset = cell_len_chars(line);
                    } else {
                        offset += line.len();
                    }
                }
            } else {
                if start > 0 {
                    breaks.push(start);
                }
                cell_offset = cell_len_chars(word);
            }
        } else if cell_offset > 0 && start > 0 {
            breaks.push(start);
            // `_cell_len(word)`, not the rstripped length: the trailing
            // whitespace moves to the next line with the word, and Rich counts
            // it against that line's budget.
            cell_offset = cell_len_chars(word);
        }
    }

    breaks
}

/// `re.finditer(r"\s*\S+\s*")`: each word with its surrounding whitespace.
///
/// Trailing whitespace with no word after it produces no match, exactly like
/// the regex — that is why a line can end without its trailing space.
fn words(chars: &[char]) -> Vec<(usize, &[char])> {
    let mut found = Vec::new();
    let mut index = 0;
    while index < chars.len() {
        let start = index;
        while index < chars.len() && chars[index].is_whitespace() {
            index += 1;
        }
        if index >= chars.len() {
            break;
        }
        while index < chars.len() && !chars[index].is_whitespace() {
            index += 1;
        }
        while index < chars.len() && chars[index].is_whitespace() {
            index += 1;
        }
        found.push((start, &chars[start..index]));
    }
    found
}

/// `chop_cells`: split a word into chunks that each fit `width` cells.
fn chop_cells(word: &[char], width: usize) -> Vec<Vec<char>> {
    let mut lines: Vec<Vec<char>> = vec![Vec::new()];
    let mut total = 0usize;
    for ch in word {
        let cells = cell_width(*ch);
        if total + cells > width {
            lines.push(vec![*ch]);
            total = cells;
        } else {
            lines.last_mut().expect("one line").push(*ch);
            total += cells;
        }
    }
    lines
}

/// `rstrip_end(width)` then `truncate(width, overflow="fold")`.
fn render_line(chars: &[char], width: usize) -> String {
    let mut line: Vec<char> = chars.to_vec();
    if line.len() > width {
        // Only whitespace *beyond* the width is removed, so a short line keeps
        // the trailing space that belongs to the next word.
        let excess = line.len() - width;
        let trailing = line
            .iter()
            .rev()
            .take_while(|ch| ch.is_whitespace())
            .count();
        let crop = trailing.min(excess);
        line.truncate(line.len() - crop);
    }
    // `truncate(width, overflow="fold")` → `set_cell_size`: a wide character
    // that straddles the boundary becomes a single space rather than vanishing.
    let mut cells = 0usize;
    let mut out = String::with_capacity(line.len());
    for ch in line {
        let advance = cell_width(ch);
        if cells + advance > width {
            if cells + 1 == width && advance == 2 {
                out.push(' ');
            }
            break;
        }
        cells += advance;
        out.push(ch);
    }
    out
}

/// Cells occupied by `text` (Rich's `cell_len`).
fn cell_len(text: &str) -> usize {
    text.chars().map(cell_width).sum()
}

/// Cells occupied by a character slice.
fn cell_len_chars(chars: &[char]) -> usize {
    chars.iter().map(|ch| cell_width(*ch)).sum()
}

/// `str.rstrip()` over a character slice.
fn trim_end_whitespace(chars: &[char]) -> &[char] {
    let end = chars
        .iter()
        .rposition(|ch| !ch.is_whitespace())
        .map(|index| index + 1)
        .unwrap_or(0);
    &chars[..end]
}

/// Cells occupied by one character (Rich's `get_character_cell_size`).
///
/// Rich uses a generated table; this is the same rule over the ranges that
/// matter for a config path and a message: control characters and combining
/// marks are zero cells, East Asian wide/fullwidth characters are two, and
/// everything else is one.
fn cell_width(ch: char) -> usize {
    let code = ch as u32;
    if code == 0 {
        return 0;
    }
    if code < 0x20 || (0x7f..0xa0).contains(&code) {
        return 0;
    }
    if is_zero_width(code) {
        return 0;
    }
    if is_wide(code) {
        return 2;
    }
    1
}

fn is_zero_width(code: u32) -> bool {
    matches!(code,
        0x0300..=0x036f      // combining diacritical marks
        | 0x0483..=0x0489
        | 0x0591..=0x05bd
        | 0x0610..=0x061a
        | 0x064b..=0x065f
        | 0x0670
        | 0x06d6..=0x06dc
        | 0x0e31 | 0x0e34..=0x0e3a | 0x0e47..=0x0e4e
        | 0x1ab0..=0x1aff
        | 0x1dc0..=0x1dff
        | 0x20d0..=0x20f0
        | 0xfe00..=0xfe0f      // variation selectors
        | 0xfe20..=0xfe2f
        | 0x200b..=0x200f      // zero-width space and joiners
        | 0x2060..=0x2064
    )
}

fn is_wide(code: u32) -> bool {
    matches!(code,
        0x1100..=0x115f
        | 0x2e80..=0x303e
        | 0x3041..=0x33ff
        | 0x3400..=0x4dbf
        | 0x4e00..=0x9fff
        | 0xa000..=0xa4cf
        | 0xa960..=0xa97f
        | 0xac00..=0xd7a3
        | 0xf900..=0xfaff
        | 0xfe10..=0xfe19
        | 0xfe30..=0xfe6f
        | 0xff00..=0xff60
        | 0xffe0..=0xffe6
        | 0x1f300..=0x1f64f
        | 0x1f900..=0x1f9ff
        | 0x20000..=0x2fffd
        | 0x30000..=0x3fffd
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The exact stdout the shipped engine produced for an empty config at
    /// `/tmp/xpst-parity-scratch-2026-11-24-long-directory-name/cfg/config.yaml`
    /// (captured with `COLUMNS` unset, i.e. width 80). Trailing spaces are part
    /// of the expectation: Rich keeps the space that belongs to the next word.
    const EMPTY_CONFIG_STDOUT: &[&str] = &[
        "Configuration error: XPST config file is empty or invalid: ",
        "/tmp/xpst-parity-scratch-2026-11-24-long-directory-name/cfg/config.yaml. It may ",
        "have been truncated by a crash or a full disk — the original file was backed up ",
        "to: ",
        "/tmp/xpst-parity-scratch-2026-11-24-long-directory-name/cfg/backups/config.yaml.",
        "corrupt_1795466493.",
    ];

    #[test]
    fn wraps_the_empty_config_message_exactly_like_rich() {
        let message = "XPST config file is empty or invalid: \
                       /tmp/xpst-parity-scratch-2026-11-24-long-directory-name/cfg/config.yaml. \
                       It may have been truncated by a crash or a full disk — the original file \
                       was backed up to: \
                       /tmp/xpst-parity-scratch-2026-11-24-long-directory-name/cfg/backups/config.yaml.corrupt_1795466493.";
        let mut out: Vec<u8> = Vec::new();
        print_error(&mut out, "Configuration error: ", message).expect("writes");
        let text = String::from_utf8(out).unwrap();
        let lines: Vec<&str> = text.strip_suffix('\n').unwrap().split('\n').collect();
        assert_eq!(lines, EMPTY_CONFIG_STDOUT);
    }

    #[test]
    fn a_short_message_is_one_line_with_no_padding() {
        let mut out: Vec<u8> = Vec::new();
        print_error(&mut out, "Configuration error: ", "boom").unwrap();
        assert_eq!(
            String::from_utf8(out).unwrap(),
            "Configuration error: boom\n"
        );
    }

    #[test]
    fn a_word_longer_than_the_line_is_folded_not_cropped() {
        let lines = wrap("abcdefghij", 4);
        assert_eq!(lines, vec!["abcd", "efgh", "ij"]);
    }

    #[test]
    fn a_word_that_fits_the_next_line_moves_whole() {
        let lines = wrap("aaa bbb", 4);
        assert_eq!(lines, vec!["aaa ", "bbb"]);
    }

    #[test]
    fn blank_lines_are_preserved() {
        assert_eq!(wrap("a\n\nb", 10), vec!["a", "", "b"]);
    }

    #[test]
    fn a_wide_character_that_straddles_the_boundary_becomes_a_space() {
        // `set_cell_size("漢", 1)` returns " ": Rich pads the half cell rather
        // than dropping the character, so a one-column console still shows a
        // placeholder.
        assert_eq!(wrap("漢", 1), vec![" "]);
        assert_eq!(wrap("a漢", 1), vec!["a", " "]);
    }

    #[test]
    fn wide_characters_take_two_cells() {
        // Four CJK characters are eight cells, so only two fit on a four-cell
        // line.
        assert_eq!(wrap("漢字漢字", 4), vec!["漢字", "漢字"]);
    }

    #[test]
    fn columns_overrides_the_default_width() {
        // The test process shares its environment with other tests, so this
        // only asserts the parsing rule, not the process-wide value.
        assert_eq!(console_width(), {
            let columns = std::env::var("COLUMNS").unwrap_or_default();
            if !columns.is_empty() && columns.chars().all(|ch| ch.is_ascii_digit()) {
                columns.parse::<usize>().unwrap_or(80).max(1)
            } else {
                80
            }
        });
    }

    #[test]
    fn the_log_block_is_a_time_level_message_table() {
        let mut out: Vec<u8> = Vec::new();
        log_error(&mut out, "XPST config file at /tmp/cfg/config.yaml is corrupted; a copy was saved to /tmp/cfg/backups/config.yaml.corrupt_1").unwrap();
        let text = String::from_utf8(out).unwrap();
        let lines: Vec<&str> = text.lines().collect();
        assert!(lines.len() > 1, "the message wraps: {text}");
        for line in &lines {
            assert_eq!(
                cell_len(line),
                80,
                "every row is padded to the console width: {line:?}"
            );
        }
        assert!(lines[0].starts_with('['), "{}", lines[0]);
        assert!(
            lines[0].contains("] ERROR    XPST config file at"),
            "{}",
            lines[0]
        );
        assert!(
            lines[1].starts_with(&" ".repeat(29)),
            "the message column starts after the time and level columns: {:?}",
            lines[1]
        );
    }
}
