//! `xpst media status` and `xpst verify-media`.
//!
//! `media status` parity target: `src/xpst/cli.py::media_status`, which prints
//! `{"bin_dir", "binaries", "ok"}` from `media_binary_status()`. Nothing there
//! downloads, spawns or probes a binary: the command answers "what would the
//! engine run", which is the question an operator asks when a re-encode fails.
//!
//! `verify-media` parity target: `src/xpst/cli.py::verify_media_cmd` →
//! `xpst.services.post_preflight.PostPreflightService.plan` with
//! `include_readiness=False` and `include_transform=--plan`. The plan assembly
//! is [`xpst_core::preflight::plan_post`]; this module resolves `-p all` the way
//! the shipped command does (`enabled_destinations`, the six-entry
//! `DESTINATION_ORDER`), renders Python's key order and applies the exit code.
//!
//! **Divergences, recorded in the ledger note:**
//!
//! * Text mode prints plain lines instead of Rich's coloured output (`--json` is
//!   the parity surface, as for every other ported command).
//! * `-p all` resolves to the *enabled* destinations, so a profile with nothing
//!   enabled verifies nothing and reports `ok: true` — the same truth the post
//!   path would give, and the reason a disabled Threads can no longer fail the
//!   exit code with `THREADS_NEEDS_URL`.
//!
//! `verify-media`'s exit code is 1 when any error was found for the selected
//! platform(s). That is behaviour, not a failure: the fixture records those rows
//! with `allow_nonzero`.
//!
//! Both payloads are built from `xpst_config::Value` so the key order and the
//! separators match `json.dumps` byte for byte.

use std::io::Write;
use std::path::{Path, PathBuf};

use clap::ArgMatches;
use xpst_config::Value as YamlValue;
use xpst_core::preflight::{
    self, Check, CheckStatus, MediaFilePlan, PlanRequest, PlatformPlan, TransformPlan,
};

use crate::commands::bootstrap;
use crate::commands::config_view::ConfigView;
use crate::commands::preflight::{
    enabled_destinations, platform_enabled, post_plan_value, transform_value, Host,
};
use crate::output::{array, boolean, object, string, write_python_json, write_text};
use crate::{exit, flag, GlobalOptions};

/// Run `media status`.
pub fn status(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let rows = xpst_media::status();
    let bin_dir = xpst_media::media_bin_dir()
        .map(|dir| dir.to_string_lossy().to_string())
        .unwrap_or_default();
    let ok = rows.iter().all(|row| row.ok);

    if json_mode {
        let binaries = object(
            rows.iter()
                .map(|row| {
                    (
                        row.name,
                        object(vec![
                            ("name", string(row.name)),
                            ("ok", boolean(row.ok)),
                            (
                                "path",
                                row.path.clone().map_or_else(crate::output::null, string),
                            ),
                            ("source", string(row.source)),
                        ]),
                    )
                })
                .collect(),
        );
        let payload = object(vec![
            ("bin_dir", string(bin_dir)),
            ("binaries", binaries),
            ("ok", boolean(ok)),
        ]);
        let _ = write_python_json(out, &payload);
        return exit::SUCCESS;
    }

    let mut text = String::new();
    for row in &rows {
        if row.ok {
            text.push_str(&format!(
                "✓ {} ({}): {}\n",
                row.name,
                row.source,
                row.path.as_deref().unwrap_or("")
            ));
        } else {
            text.push_str(&format!("• {}: not found\n", row.name));
        }
    }
    if !ok {
        text.push_str(
            "Run xpst media fetch to download a verified static build, or install ffmpeg \
             (macOS: brew install ffmpeg).",
        );
    }
    let _ = write_text(out, text.trim_end());
    // `err` is unused: `media status` has no failure path that is not a missing
    // binary, which is reported in the payload rather than as an error.
    let _ = err;
    exit::SUCCESS
}

/// Run `verify-media`.
pub fn verify_media(
    global: &GlobalOptions,
    leaf: Option<&ArgMatches>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32 {
    let json_mode = global.json || leaf.is_some_and(|m| flag(m, "opt:json"));
    let file = crate::leaf_string(leaf, "arg:file").unwrap_or_default();
    let platform = crate::leaf_string(leaf, "opt:platform").unwrap_or_else(|| "all".to_owned());
    let show_plan = leaf.is_some_and(|m| flag(m, "opt:plan"));

    // `click.Path(exists=True)` validates the argument before the command body
    // runs, so a missing FILE is a usage error (exit 2) with Click's wording —
    // not a `MEDIA_NOT_FOUND` report.
    if !Path::new(&file).exists() {
        let _ = writeln!(
            err,
            "Usage: xpst verify-media [OPTIONS] FILE\n\
             Try 'xpst verify-media --help' for help.\n\n\
             Error: Invalid value for 'FILE': Path '{file}' does not exist."
        );
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
            let _ = writeln!(err, "xpst verify-media: {message}");
            return exit::GENERAL;
        }
    };

    // `-p all` is the enabled destinations; an explicit `-p` is verified even
    // when the destination is switched off, and the payload says so.
    let platforms: Vec<String> = if platform == "all" {
        enabled_destinations(&view)
    } else {
        vec![platform.clone()]
    };
    let disabled: Vec<String> = platforms
        .iter()
        .filter(|name| !platform_enabled(&view, name))
        .cloned()
        .collect();

    let host = Host::new(&view, &config_dir, &env);
    let request = PlanRequest {
        media_paths: vec![file.clone()],
        caption: String::new(),
        per_platform_captions: Vec::new(),
        content_type: None,
        requested: Some(platforms.clone()),
        all_connected: false,
        include_transform: show_plan,
        include_readiness: false,
        probe_media: true,
        visibility: None,
        // `PostPlanRequest.check_loudness` defaults on, and this is the command
        // that needs it.
        check_loudness: true,
    };
    let plan = preflight::plan_post(&host, &request, &platforms);

    let reports: Vec<MediaReport> = platforms
        .iter()
        .filter_map(|name| plan.platform(name).map(|item| report_for(item, &file)))
        .collect();
    let plans: Vec<TransformPlan> = platforms
        .iter()
        .map(|name| {
            plan.platform(name)
                .and_then(PlatformPlan::transform)
                .cloned()
                .unwrap_or_else(|| TransformPlan::transcode(name, "media transform unavailable"))
        })
        .collect();
    let ok = plan.ok();

    if json_mode {
        let mut entries: Vec<(&str, YamlValue)> = vec![
            ("file", string(file.clone())),
            ("ok", boolean(ok)),
            (
                "reports",
                array(reports.iter().map(MediaReport::value).collect()),
            ),
        ];
        if platform != "all" && !disabled.is_empty() {
            entries.push((
                "disabled_platforms",
                array(disabled.iter().map(|name| string(name.clone())).collect()),
            ));
        }
        if show_plan {
            entries.push(("post_plan", post_plan_value(&plan)));
            entries.push(("plans", array(plans.iter().map(transform_value).collect())));
        }
        let _ = write_python_json(out, &object(entries));
    } else {
        let mut text = format!("Verifying {file}\n");
        for report in &reports {
            text.push_str(&report.render());
        }
        if platform != "all" && !disabled.is_empty() {
            text.push_str(&format!(
                "Note: {} {} disabled in local configuration — this check is informational; a \
                 disabled destination would not be posted to at all.\n",
                disabled.join(", "),
                if disabled.len() == 1 { "is" } else { "are" }
            ));
        }
        if show_plan {
            text.push_str("Transformation plan (dry run):\n");
            for item in &plans {
                text.push_str(&format!("  • {}\n", describe_plan(item)));
            }
        }
        if !ok {
            text.push_str("Pre-flight FAILED — fix the [FAIL] items before uploading.\n");
        } else if reports.iter().any(MediaReport::has_warnings) {
            text.push_str(
                "Uploadable, but warnings above mean the platform will re-encode/adjust.\n",
            );
        } else {
            text.push_str("All checks passed — max fidelity for the selected platform(s).\n");
        }
        let _ = write_text(out, text.trim_end());
    }

    if !ok {
        return exit::GENERAL;
    }
    exit::SUCCESS
}

/// `MediaReport` — the per-destination verdict `verify-media` reports.
struct MediaReport {
    path: String,
    platform: String,
    checks: Vec<Check>,
}

impl MediaReport {
    fn ok(&self) -> bool {
        !self
            .checks
            .iter()
            .any(|check| check.status == CheckStatus::Error)
    }

    fn has_warnings(&self) -> bool {
        self.checks
            .iter()
            .any(|check| check.status == CheckStatus::Warn)
    }

    /// `MediaReport.to_dict()` — the CLI calls it with `include_probe` off, so
    /// the report carries no `probe` key at all.
    ///
    /// The probe document is still published, but only inside the `--plan`
    /// block (`media_spec.probe` / `ffprobe`), which is where Python puts it.
    fn value(&self) -> YamlValue {
        object(vec![
            ("path", string(self.path.clone())),
            ("platform", string(self.platform.clone())),
            ("ok", boolean(self.ok())),
            (
                "checks",
                array(
                    self.checks
                        .iter()
                        .map(|check| {
                            object(vec![
                                ("name", string(check.name.clone())),
                                ("status", string(check.status.as_str())),
                                ("detail", string(check.detail.clone())),
                            ])
                        })
                        .collect(),
                ),
            ),
        ])
    }

    /// `format_report()`: the path/display-name header, then one line per check.
    fn render(&self) -> String {
        let mut text = format!(
            "{} — {}\n",
            self.path,
            xpst_media::specs::destination_display_name(&self.platform)
        );
        for check in &self.checks {
            let mark = match check.status {
                CheckStatus::Ok => "[ok]  ",
                CheckStatus::Warn => "[WARN]",
                CheckStatus::Error => "[FAIL]",
            };
            text.push_str(&format!("  {mark} {}: {}\n", check.name, check.detail));
        }
        text
    }
}

/// The report `verify_media_cmd` builds from one platform's plan.
///
/// The spec checks come from the media item; a hard blocker is added only when
/// it carries a fact the checks do not already show. Matching on the message as
/// well as the code matters for checks whose preflight code differs from the
/// check name (`modality` → `MEDIA_MODALITY_UNSUPPORTED`): without it the same
/// refusal was printed twice.
fn report_for(plan: &PlatformPlan, media_path: &str) -> MediaReport {
    let item: Option<&MediaFilePlan> = plan.media.first();
    let mut checks: Vec<Check> = item.map(|item| item.checks.clone()).unwrap_or_default();
    let known_codes: Vec<String> = checks.iter().map(|check| check.name.clone()).collect();
    let known_messages: Vec<String> = checks.iter().map(|check| check.detail.clone()).collect();
    for issue in &plan.hard_blockers {
        if issue.media_path.as_deref() != Some(media_path) {
            continue;
        }
        if known_codes.contains(&issue.code) {
            continue;
        }
        if known_messages.contains(&issue.message) {
            continue;
        }
        checks.push(Check {
            name: issue.code.clone(),
            status: CheckStatus::Error,
            detail: issue.message.clone(),
        });
    }
    MediaReport {
        path: item.map_or_else(|| media_path.to_owned(), |item| item.path.clone()),
        platform: plan.platform.clone(),
        checks,
    }
}

/// `describe_plan()`: one line per destination.
fn describe_plan(plan: &TransformPlan) -> String {
    let action = match plan.action.as_str() {
        "passthrough" => "PASSTHROUGH (upload source bytes as-is)",
        "remux" => "REMUX (stream copy, zero quality loss)",
        _ => "TRANSCODE (re-encode via platform profile)",
    };
    let detail = plan
        .remux_cmd
        .as_ref()
        .map(|command| command.join(" "))
        .or_else(|| plan.transcode_summary.clone())
        .unwrap_or_default();
    let loud = if plan.action == "transcode" {
        plan.loudness_target_lufs
            .map(|target| format!(" | audio: EBU R128 loudnorm to {target:.0} LUFS"))
            .unwrap_or_default()
    } else {
        String::new()
    };
    let detail = if detail.is_empty() {
        String::new()
    } else {
        format!(" | {detail}")
    };
    format!("{}: {action}{detail}{loud}", plan.platform)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn check(name: &str, status: CheckStatus, detail: &str) -> Check {
        Check {
            name: name.to_owned(),
            status,
            detail: detail.to_owned(),
        }
    }

    fn readiness() -> preflight::PlatformReadiness {
        preflight::PlatformReadiness {
            enabled: true,
            auth: preflight::AuthReadiness::unchecked(),
            quota: preflight::QuotaReadiness::unchecked(),
        }
    }

    fn media_item(
        path: &str,
        checks: Vec<Check>,
        blockers: Vec<preflight::PreflightIssue>,
    ) -> MediaFilePlan {
        MediaFilePlan {
            path: path.to_owned(),
            file_type: "file".to_owned(),
            exists: Some(true),
            is_file: Some(true),
            size_bytes: Some(64),
            duration_seconds: None,
            aspect_ratio: None,
            checks,
            probe: None,
            transform: None,
            warnings: Vec::new(),
            hard_blockers: blockers,
        }
    }

    #[test]
    fn a_report_renders_pythons_marks() {
        let report = MediaReport {
            path: "/tmp/clip.mp4".to_owned(),
            platform: "threads".to_owned(),
            checks: vec![
                check("container", CheckStatus::Ok, ".mp4 vs accepted .mp4"),
                check("fps", CheckStatus::Warn, "120.0 vs cap 60"),
                check("video_stream", CheckStatus::Error, "NO real video stream"),
            ],
        };
        assert_eq!(
            report.render(),
            "/tmp/clip.mp4 — Threads\n  \
             [ok]   container: .mp4 vs accepted .mp4\n  \
             [WARN] fps: 120.0 vs cap 60\n  \
             [FAIL] video_stream: NO real video stream\n"
        );
        assert!(!report.ok());
        assert!(report.has_warnings());
    }

    #[test]
    fn a_hard_blocker_already_shown_by_a_check_is_not_repeated() {
        let blocker =
            preflight::PreflightIssue::blocker("MEDIA_SPEC_CONTAINER", ".mkv vs accepted .mp4")
                .with_media_path("/tmp/clip.mp4");
        let plan = PlatformPlan {
            platform: "youtube".to_owned(),
            effective_caption: String::new(),
            media: vec![media_item(
                "/tmp/clip.mp4",
                vec![check(
                    "container",
                    CheckStatus::Error,
                    ".mkv vs accepted .mp4",
                )],
                vec![blocker.clone()],
            )],
            constraints: None,
            readiness: readiness(),
            warnings: Vec::new(),
            hard_blockers: vec![blocker],
        };
        let report = report_for(&plan, "/tmp/clip.mp4");
        assert_eq!(report.checks.len(), 1, "the same refusal was printed twice");
        assert_eq!(report.checks[0].name, "container");
    }

    #[test]
    fn a_blocker_with_no_matching_check_is_added() {
        let blocker = preflight::PreflightIssue::blocker(
            "MEDIA_NOT_FOUND",
            "Media path does not exist: /tmp/missing.mp4",
        )
        .with_media_path("/tmp/missing.mp4");
        let plan = PlatformPlan {
            platform: "youtube".to_owned(),
            effective_caption: String::new(),
            media: vec![media_item(
                "/tmp/missing.mp4",
                Vec::new(),
                vec![blocker.clone()],
            )],
            constraints: None,
            readiness: readiness(),
            warnings: Vec::new(),
            hard_blockers: vec![blocker],
        };
        let report = report_for(&plan, "/tmp/missing.mp4");
        assert_eq!(report.checks.len(), 1);
        assert_eq!(report.checks[0].name, "MEDIA_NOT_FOUND");
        assert_eq!(report.checks[0].status, CheckStatus::Error);
    }

    #[test]
    fn describe_plan_matches_pythons_wording() {
        let mut plan = TransformPlan::transcode("youtube", "hevc is not h264");
        plan.transcode_summary = Some("H.264 crf 20, long edge 1920".to_owned());
        plan.loudness_target_lufs = Some(-14.0);
        assert_eq!(
            describe_plan(&plan),
            "youtube: TRANSCODE (re-encode via platform profile) | H.264 crf 20, long edge 1920 | \
             audio: EBU R128 loudnorm to -14 LUFS"
        );
        let passthrough = TransformPlan {
            platform: "x".to_owned(),
            action: "passthrough".to_owned(),
            reasons: Vec::new(),
            remux_cmd: None,
            transcode_summary: None,
            loudness_target_lufs: Some(-16.0),
            loudness_normalized: false,
            probe_error: None,
        };
        assert_eq!(
            describe_plan(&passthrough),
            "x: PASSTHROUGH (upload source bytes as-is)"
        );
    }
}
