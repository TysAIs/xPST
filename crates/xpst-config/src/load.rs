//! Python's `XPSTConfig.load` read path, including the corrupt-config family.
//!
//! `ConfigDocument::read` is the *pure* reader: it parses and reports, and is
//! what `xpst config show` uses (Python's `config_show` also reads the file
//! directly, with its own error text). This module is the other half — the
//! behaviour of `XPSTConfig.load`, which every command that calls
//! `load_config()` inherits:
//!
//! * a file that is empty, not valid UTF-8, or not parseable is **backed up**
//!   to `<config dir>/backups/config.yaml.corrupt_<epoch>` before the error is
//!   raised, so a crash or a full disk cannot destroy the user's only copy;
//! * the message names the backup, because "your config is broken" without
//!   "here is the copy" is a message that loses data;
//! * a document that parses but is not a mapping is a *different* error with a
//!   different message, and is **not** backed up (nothing was lost — the file
//!   is intact, it is simply the wrong shape).
//!
//! The strings here are copied from `src/xpst/config.py` (`XPSTConfig.load`,
//! `_backup_corrupt_config_file`) rather than paraphrased: they are what the
//! shipped engine prints, and `crates/xpst-cli/tests/local_surface.rs` holds
//! the binary to them byte for byte.

use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use crate::{yaml, ConfigDocument, Value};

/// `pathlib.Path(config_path)`: the normalisation Python applies before the
/// path is used for the message, the backup directory and the existence check.
///
/// `PurePath` collapses repeated separators, drops `.` components and strips a
/// trailing separator. It does **not** resolve `..` (that is `resolve()`), and
/// on POSIX it keeps a leading `//` (the implementation-defined double slash)
/// while collapsing three or more to one. On Windows both `/` and `\` are
/// separators, a `C:` drive prefix is kept, and the result renders with `\`.
///
/// Two Windows edges are not reproduced because no config path reaches them:
/// a verbatim `\\?\` prefix, and a UNC root with no tail (`\\server\share`
/// gains a trailing separator in `PureWindowsPath`). Three or more leading
/// separators also collapse to two here where `PureWindowsPath` keeps them.
///
/// This matters because `--config ./config.yaml` is what shell completion
/// hands a user: Python prints `config.yaml` and backs up to `backups/…`, so a
/// raw `./config.yaml` would be a different answer. A trailing slash is worse
/// than cosmetic — `Path::new("config.yaml/").exists()` is false on Linux, so
/// the raw path takes the "explicit path missing" branch instead of reading
/// the file.
#[must_use]
pub fn python_path(path: &Path) -> PathBuf {
    match path.to_str() {
        Some(raw) => PathBuf::from(python_path_text(raw, cfg!(windows))),
        // A non-UTF-8 path cannot come from `argv` (Python would have decoded
        // it), so leave it alone rather than lossily rewriting it.
        None => path.to_path_buf(),
    }
}

/// The string half of [`python_path`], parameterised so the Windows rules can
/// be tested on Linux.
fn python_path_text(raw: &str, windows: bool) -> String {
    let sep = if windows { '\\' } else { '/' };
    let is_sep = |ch: char| ch == '/' || (windows && ch == '\\');
    let chars: Vec<char> = raw.chars().collect();
    let mut index = 0;
    let mut prefix = String::new();

    // `PureWindowsPath` keeps a drive (`C:`) verbatim, including a
    // drive-relative path with no root (`C:config.yaml`).
    if windows && chars.len() >= 2 && chars[1] == ':' && chars[0].is_ascii_alphabetic() {
        prefix.push(chars[0]);
        prefix.push(':');
        index = 2;
    }

    let mut root = String::new();
    if windows {
        if index < chars.len() && is_sep(chars[index]) {
            let mut slashes = 0;
            while index < chars.len() && is_sep(chars[index]) {
                slashes += 1;
                index += 1;
            }
            // After a drive only one root separator survives (`C:\\a` is
            // `C:\a`); a UNC path keeps its two (`\\server\share`).
            root.push_str(if slashes >= 2 && prefix.is_empty() {
                "\\\\"
            } else {
                "\\"
            });
        }
    } else if index < chars.len() && chars[index] == '/' {
        let mut slashes = 0;
        while index < chars.len() && chars[index] == '/' {
            slashes += 1;
            index += 1;
        }
        root.push_str(if slashes == 2 { "//" } else { "/" });
    }

    let mut parts: Vec<String> = Vec::new();
    let mut current = String::new();
    for &ch in &chars[index..] {
        if is_sep(ch) {
            push_component(&mut parts, &mut current);
        } else {
            current.push(ch);
        }
    }
    push_component(&mut parts, &mut current);

    let mut out = prefix;
    out.push_str(&root);
    for (position, part) in parts.iter().enumerate() {
        if position > 0 {
            out.push(sep);
        }
        out.push_str(part);
    }
    // `PurePath("")` and `PurePath(".")` both render as `.`.
    if out.is_empty() {
        out.push('.');
    }
    out
}

/// Keep a component unless it is empty (`a//b`) or `.` (`a/./b`); `..` stays.
fn push_component(parts: &mut Vec<String>, current: &mut String) {
    if !current.is_empty() && current != "." {
        parts.push(std::mem::take(current));
    } else {
        current.clear();
    }
}

/// A failure from the config *load* path.
///
/// Deliberately separate from [`crate::ConfigError`]: that type is the pure
/// reader's error (used by `config show`, whose Python counterpart has its own
/// wording), while these variants carry the backup path and the exact
/// `XPSTConfig.load` message.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LoadError {
    /// The file exists but parses to null: an empty file, `~`, or `null`.
    Empty {
        path: String,
        /// Where the original was copied, or `None` when the copy failed.
        backup: Option<String>,
    },
    /// The document parsed, but it is not a mapping (`- 1`, `"text"`, `42`).
    NotAMapping {
        path: String,
        /// Python's `type(value).__name__` for the parsed document.
        kind: String,
    },
    /// The parser rejected the document.
    InvalidYaml {
        path: String,
        /// The parser's own message, which Python interpolates verbatim.
        message: String,
        backup: Option<String>,
    },
    /// The bytes are not valid UTF-8 (a binary file, or a truncated write).
    NotUtf8 {
        path: String,
        backup: Option<String>,
    },
    /// The file could not be read at all (permissions, I/O error).
    Io { path: String, message: String },
    /// The file does not exist.
    NotFound { path: String },
}

impl LoadError {
    /// The backup this failure wrote, when it wrote one.
    #[must_use]
    pub fn backup(&self) -> Option<&str> {
        match self {
            LoadError::Empty { backup, .. }
            | LoadError::InvalidYaml { backup, .. }
            | LoadError::NotUtf8 { backup, .. } => backup.as_deref(),
            _ => None,
        }
    }

    /// The line Python's `_backup_corrupt_config_file` logs to stderr.
    ///
    /// `logger.error("XPST config file at %s is corrupted; a copy was saved to
    /// %s", cfg_path, dest)` — emitted before the error is raised, so a user
    /// watching the terminal sees the copy being made.
    #[must_use]
    pub fn backup_notice(&self) -> Option<String> {
        let backup = self.backup()?;
        let path = match self {
            LoadError::Empty { path, .. }
            | LoadError::InvalidYaml { path, .. }
            | LoadError::NotUtf8 { path, .. } => path,
            _ => return None,
        };
        Some(format!(
            "XPST config file at {path} is corrupted; a copy was saved to {backup}"
        ))
    }
}

impl std::fmt::Display for LoadError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            // `f"...backed up to: {backup}."` renders a failed backup as
            // `None`, which is what the shipped engine prints.
            LoadError::Empty { path, backup } => write!(
                f,
                "XPST config file is empty or invalid: {path}. It may have been truncated by a \
                 crash or a full disk — the original file was backed up to: {}.",
                backup_text(backup)
            ),
            LoadError::NotAMapping { path, kind } => {
                write!(f, "XPST config must be a mapping, got {kind}: {path}")
            }
            LoadError::InvalidYaml {
                path,
                message,
                backup,
            } => write!(
                f,
                "XPST config file could not be parsed: {path} ({message}). The original file was \
                 backed up to: {}. Fix or restore the file, then retry.",
                backup_text(backup)
            ),
            LoadError::NotUtf8 { path, backup } => write!(
                f,
                "XPST config file is not valid UTF-8 (binary or corrupted?): {path}. The original \
                 file was backed up to: {}. Fix or restore the file, then retry.",
                backup_text(backup)
            ),
            LoadError::Io { path, message } => {
                write!(f, "XPST config file could not be read: {path} ({message})")
            }
            LoadError::NotFound { path } => write!(f, "XPST config file not found: {path}"),
        }
    }
}

impl std::error::Error for LoadError {}

fn backup_text(backup: &Option<String>) -> &str {
    backup.as_deref().unwrap_or("None")
}

impl ConfigDocument {
    /// Read a config file the way `XPSTConfig.load` does.
    ///
    /// Differences from [`ConfigDocument::read`], all of them Python's:
    ///
    /// * the file's permissions are tightened to `0600` when it is group- or
    ///   world-readable (`_tighten_config_permissions`), because it holds
    ///   account details;
    /// * a corrupt file is copied to `backups/config.yaml.corrupt_<epoch>`
    ///   before the error is returned;
    /// * an empty document and a non-mapping document are distinct errors.
    ///
    /// Not reproduced, and named here so nobody reads the absence as an
    /// oversight:
    ///
    /// * `copy2`'s mtime preservation — the bytes and the mode are copied, the
    ///   backup's mtime is the copy's;
    /// * the schema migration that follows a successful load;
    /// * the *inner* text of the parse error. Python interpolates PyYAML's
    ///   `MarkedYAMLError` block (`while parsing a flow node ... in "<unicode
    ///   string>", line 2, column 1`); this crate interpolates serde-saphyr's
    ///   one-line message. The path, the backup and the "fix or restore"
    ///   instruction around it are identical, and the ledger records the
    ///   difference. The same class covers an OS error: `--config` pointing at
    ///   a directory reads `([Errno 21] Is a directory: '<path>')` in Python
    ///   and `(Is a directory (os error 21))` here;
    /// * `ensure_config_dir_usable`, which Python runs *before* reading. A
    ///   config in a directory the user cannot write to reports
    ///   `XPST config directory … is not writable by uid N. Fix: chmod u+rwx …`
    ///   there, while this loader reports the empty-config message with
    ///   `backed up to: None` (the copy failed). Porting the precondition
    ///   would change valid-config behaviour, which this card forbids, so it
    ///   is recorded rather than implemented;
    /// * the narrow-console layout: at `COLUMNS` ≤ 30 Rich shrinks the time and
    ///   level columns, while `rich::log_error` keeps the full 29-cell prefix
    ///   and overflows the width. stdout still matches at every width.
    pub fn load(path: &Path) -> Result<Self, LoadError> {
        // `cfg_path: Path = Path(config_path)` — the message, the backup
        // directory and the existence check all use the normalised form.
        let path = python_path(path);
        let path = path.as_path();
        let path_text = path.display().to_string();
        let bytes = std::fs::read(path).map_err(|err| {
            if err.kind() == std::io::ErrorKind::NotFound {
                LoadError::NotFound {
                    path: path_text.clone(),
                }
            } else {
                LoadError::Io {
                    path: path_text.clone(),
                    message: err.to_string(),
                }
            }
        })?;

        // Python tightens the mode *before* reading, so the backup inherits
        // 0600 rather than whatever the file had.
        tighten_config_permissions(path);

        // `raw.decode("utf-8-sig")`: a BOM is stripped, invalid bytes are an
        // error (not a lossy replacement).
        let bytes = bytes.strip_prefix(b"\xef\xbb\xbf").unwrap_or(&bytes);
        let text = match std::str::from_utf8(bytes) {
            Ok(text) => text,
            Err(_) => {
                let backup = backup_corrupt_config_file(path);
                return Err(LoadError::NotUtf8 {
                    path: path_text,
                    backup,
                });
            }
        };

        let value: Value = match yaml::from_str(text) {
            Ok(value) => value,
            Err(err) => {
                let backup = backup_corrupt_config_file(path);
                return Err(LoadError::InvalidYaml {
                    path: path_text,
                    message: err.to_string(),
                    backup,
                });
            }
        };

        match value {
            Value::Null => {
                let backup = backup_corrupt_config_file(path);
                Err(LoadError::Empty {
                    path: path_text,
                    backup,
                })
            }
            Value::Mapping(_) => Ok(ConfigDocument {
                path: path_text,
                value,
            }),
            other => Err(LoadError::NotAMapping {
                path: path_text,
                kind: other.python_type_name().to_string(),
            }),
        }
    }
}

/// `_backup_corrupt_config_file`: copy the file beside itself, never move it.
///
/// Returns the backup path, or `None` when the copy failed — Python returns
/// `None` there too and still raises, so a read-only profile reports the
/// corruption instead of dying on the backup.
fn backup_corrupt_config_file(path: &Path) -> Option<String> {
    let dir = path.parent()?.join("backups");
    std::fs::create_dir_all(&dir).ok()?;
    let epoch = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    let dest: PathBuf = dir.join(format!("config.yaml.corrupt_{epoch}"));
    // `std::fs::copy` carries the permission bits across, which is the part of
    // `shutil.copy2` a user can observe.
    std::fs::copy(path, &dest).ok()?;
    Some(dest.display().to_string())
}

/// `_tighten_config_permissions`: make the config owner-only, silently.
///
/// A load must not print, so a failed chmod is ignored here; Python logs a
/// warning, which the CLI layer does not reproduce (the file is still readable
/// and the load still succeeds).
fn tighten_config_permissions(path: &Path) {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let Ok(metadata) = std::fs::metadata(path) else {
            return;
        };
        let mode = metadata.permissions().mode() & 0o777;
        if mode & 0o077 == 0 {
            return;
        }
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
    }
    #[cfg(not(unix))]
    {
        let _ = path;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Scratch {
        dir: PathBuf,
    }

    impl Scratch {
        fn new(name: &str) -> Self {
            let dir = std::env::temp_dir()
                .join(format!("xpst-config-load-{}-{name}", std::process::id()));
            let _ = std::fs::remove_dir_all(&dir);
            std::fs::create_dir_all(&dir).expect("scratch dir");
            Self { dir }
        }

        fn write(&self, name: &str, bytes: &[u8]) -> PathBuf {
            let path = self.dir.join(name);
            std::fs::write(&path, bytes).expect("seed file");
            path
        }
    }

    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.dir);
        }
    }

    fn backups(dir: &Path) -> Vec<PathBuf> {
        let mut found: Vec<PathBuf> = std::fs::read_dir(dir.join("backups"))
            .map(|entries| {
                entries
                    .flatten()
                    .map(|entry| entry.path())
                    .filter(|path| {
                        path.file_name()
                            .and_then(|name| name.to_str())
                            .is_some_and(|name| name.starts_with("config.yaml.corrupt_"))
                    })
                    .collect()
            })
            .unwrap_or_default();
        found.sort();
        found
    }

    #[test]
    fn an_empty_file_is_backed_up_and_named_in_the_message() {
        let scratch = Scratch::new("empty");
        let path = scratch.write("config.yaml", b"");
        let error = ConfigDocument::load(&path).unwrap_err();
        let backup = error.backup().expect("an empty config is backed up");
        assert!(
            backup.contains("backups/config.yaml.corrupt_"),
            "backup path: {backup}"
        );
        assert_eq!(
            std::fs::read(backup).expect("backup exists"),
            b"",
            "the backup is a copy of the original"
        );
        let text = error.to_string();
        assert!(
            text.starts_with("XPST config file is empty or invalid: "),
            "{text}"
        );
        assert!(
            text.contains("It may have been truncated by a crash or a full disk — "),
            "{text}"
        );
        assert!(
            text.ends_with(&format!("the original file was backed up to: {backup}.")),
            "{text}"
        );
        assert_eq!(
            error.backup_notice().as_deref(),
            Some(
                format!(
                    "XPST config file at {} is corrupted; a copy was saved to {backup}",
                    path.display()
                )
                .as_str()
            )
        );
    }

    #[test]
    fn a_non_mapping_document_is_not_backed_up() {
        let scratch = Scratch::new("non-mapping");
        let path = scratch.write("config.yaml", b"- 1\n");
        let error = ConfigDocument::load(&path).unwrap_err();
        assert_eq!(
            error.to_string(),
            format!(
                "XPST config must be a mapping, got list: {}",
                path.display()
            )
        );
        assert!(
            error.backup().is_none(),
            "nothing was lost, so nothing is copied"
        );
        assert!(backups(&scratch.dir).is_empty());
    }

    #[test]
    fn a_non_mapping_document_names_pythons_type() {
        for (body, kind) in [
            ("- 1\n", "list"),
            ("just a string\n", "str"),
            ("42\n", "int"),
            ("1.5\n", "float"),
            ("true\n", "bool"),
            ("2026-01-01\n", "date"),
        ] {
            let scratch = Scratch::new(&format!("kind-{kind}"));
            let path = scratch.write("config.yaml", body.as_bytes());
            let error = ConfigDocument::load(&path).unwrap_err();
            assert!(
                error.to_string().contains(&format!("got {kind}: ")),
                "{body:?} should read as {kind}: {error}"
            );
        }
    }

    #[test]
    fn invalid_yaml_is_backed_up_with_the_parse_message() {
        let scratch = Scratch::new("invalid-yaml");
        let path = scratch.write("config.yaml", b"accounts: [\n");
        let error = ConfigDocument::load(&path).unwrap_err();
        let backup = error.backup().expect("invalid YAML is backed up");
        assert_eq!(std::fs::read(backup).unwrap(), b"accounts: [\n");
        let text = error.to_string();
        assert!(
            text.starts_with(&format!(
                "XPST config file could not be parsed: {} (",
                path.display()
            )),
            "{text}"
        );
        assert!(text.contains(&format!("backed up to: {backup}.")), "{text}");
        assert!(
            text.ends_with("Fix or restore the file, then retry."),
            "{text}"
        );
    }

    #[test]
    fn invalid_utf8_is_backed_up_and_named() {
        let scratch = Scratch::new("not-utf8");
        let path = scratch.write("config.yaml", b"version: 4\n\xff\xfe\n");
        let error = ConfigDocument::load(&path).unwrap_err();
        let backup = error.backup().expect("invalid UTF-8 is backed up");
        assert_eq!(std::fs::read(backup).unwrap(), b"version: 4\n\xff\xfe\n");
        let text = error.to_string();
        assert!(
            text.starts_with(&format!(
                "XPST config file is not valid UTF-8 (binary or corrupted?): {}.",
                path.display()
            )),
            "{text}"
        );
    }

    #[test]
    fn a_utf8_bom_is_stripped_like_utf_8_sig() {
        let scratch = Scratch::new("bom");
        let path = scratch.write("config.yaml", b"\xef\xbb\xbfversion: 4\n");
        let document = ConfigDocument::load(&path).expect("a BOM is not corruption");
        assert_eq!(document.version(), 4);
        assert!(backups(&scratch.dir).is_empty());
    }

    #[test]
    fn a_mapping_loads_and_writes_nothing() {
        let scratch = Scratch::new("valid");
        let path = scratch.write("config.yaml", b"version: 4\naccounts: {}\n");
        let document = ConfigDocument::load(&path).expect("valid config");
        assert_eq!(document.version(), 4);
        assert!(backups(&scratch.dir).is_empty());
        assert_eq!(std::fs::read(&path).unwrap(), b"version: 4\naccounts: {}\n");
    }

    #[test]
    fn a_missing_file_is_not_found_and_writes_nothing() {
        let scratch = Scratch::new("missing");
        let path = scratch.dir.join("absent.yaml");
        let error = ConfigDocument::load(&path).unwrap_err();
        assert!(matches!(error, LoadError::NotFound { .. }));
        assert_eq!(
            error.to_string(),
            format!("XPST config file not found: {}", path.display())
        );
        assert!(!scratch.dir.join("backups").exists());
    }

    #[cfg(unix)]
    #[test]
    fn a_group_readable_config_is_tightened_before_the_backup() {
        use std::os::unix::fs::PermissionsExt;
        let scratch = Scratch::new("tighten");
        let path = scratch.write("config.yaml", b"");
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o644)).unwrap();
        let error = ConfigDocument::load(&path).unwrap_err();
        let mode = std::fs::metadata(&path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600, "the config holds account details");
        let backup = error.backup().expect("backed up");
        let backup_mode = std::fs::metadata(backup).unwrap().permissions().mode() & 0o777;
        assert_eq!(backup_mode, 0o600, "the copy inherits the tightened mode");
    }

    /// `pathlib.PurePosixPath(...)` answers, captured from CPython 3.13.
    ///
    /// The point of the table is the three shapes a shell hands a user:
    /// `./config.yaml` (completion), `a//config.yaml` (a doubled separator)
    /// and `config.yaml/` (a trailing slash, which `Path::exists()` rejects on
    /// Linux even though the file is there).
    #[test]
    fn python_path_normalises_like_pathlib() {
        for (raw, expected) in [
            ("config.yaml", "config.yaml"),
            ("./config.yaml", "config.yaml"),
            ("a//config.yaml", "a/config.yaml"),
            ("config.yaml/", "config.yaml"),
            ("a/./b/config.yaml", "a/b/config.yaml"),
            ("a/../b/config.yaml", "a/../b/config.yaml"),
            ("//a/config.yaml", "//a/config.yaml"),
            ("///a/config.yaml", "/a/config.yaml"),
            ("//", "//"),
            ("///", "/"),
            ("", "."),
            (".", "."),
            ("./", "."),
            ("a//", "a"),
            ("a/./", "a"),
            ("a/b/..", "a/b/.."),
            ("a/./../b", "a/../b"),
            ("a//b//c", "a/b/c"),
            ("././a", "a"),
            ("a/././b", "a/b"),
        ] {
            assert_eq!(
                python_path_text(raw, false),
                expected,
                "PurePosixPath({raw:?})"
            );
        }
    }

    /// The Windows rules, exercised on Linux: both separators split, a drive
    /// prefix survives, and the result renders with `\`. The three edges named
    /// in [`python_path`]'s doc comment are deliberately absent.
    #[test]
    fn python_path_normalises_windows_separators() {
        for (raw, expected) in [
            ("C:\\cfg\\config.yaml", "C:\\cfg\\config.yaml"),
            ("C:/cfg//config.yaml", "C:\\cfg\\config.yaml"),
            ("C:\\\\cfg\\config.yaml", "C:\\cfg\\config.yaml"),
            ("C:config.yaml", "C:config.yaml"),
            (".\\cfg\\config.yaml", "cfg\\config.yaml"),
            ("cfg\\config.yaml\\", "cfg\\config.yaml"),
            (
                "\\\\server\\share\\config.yaml",
                "\\\\server\\share\\config.yaml",
            ),
            ("cfg\\..\\config.yaml", "cfg\\..\\config.yaml"),
        ] {
            assert_eq!(
                python_path_text(raw, true),
                expected,
                "windows path {raw:?}"
            );
        }
    }

    /// The loader itself uses the normalised path for the message *and* the
    /// backup directory, not just for the existence check.
    #[test]
    fn load_reports_and_backs_up_the_normalised_path() {
        let scratch = Scratch::new("normalised");
        std::fs::create_dir_all(scratch.dir.join("cfg")).unwrap();
        std::fs::write(scratch.dir.join("cfg/config.yaml"), b"").unwrap();
        let raw = format!("{}/./cfg//config.yaml", scratch.dir.display());
        let error = ConfigDocument::load(Path::new(&raw)).unwrap_err();
        let normalised = scratch.dir.join("cfg/config.yaml");
        let text = error.to_string();
        assert!(
            text.contains(&format!("empty or invalid: {}.", normalised.display())),
            "the message names the normalised path: {text}"
        );
        assert!(
            !text.contains("//") && !text.contains("/./"),
            "no raw separators survive: {text}"
        );
        let backup = error.backup().expect("backed up");
        assert_eq!(
            Path::new(backup).parent().unwrap(),
            scratch.dir.join("cfg/backups"),
            "the copy lands beside the normalised file"
        );
    }
}
