// Media-preview helpers for the composer.
//
// Everything here is pure so the preview *plan* can be tested without a DOM:
// given a media item the composer asks what to render, and the answer is
// always a URL for the engine's range-aware stream route (or its cached
// thumbnail) — never the file's bytes. That is the whole point: selecting a
// 2 GB video must not read it, decode it, or copy it.

import { formatBytes } from "./firstRun.js";

/** Video extensions the engine will stream. Mirrors xpst.sources.local. */
export const VIDEO_EXTENSIONS = [".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v"];

/** Image extensions the engine will stream. Mirrors xpst.sources.local. */
export const IMAGE_EXTENSIONS = [".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"];

export const MEDIA_EXTENSIONS = [...VIDEO_EXTENSIONS, ...IMAGE_EXTENSIONS];

/** Longest edge requested from the thumbnail route. */
export const THUMBNAIL_WIDTH = 640;

function extensionOf(path) {
  const name = String(path ?? "");
  const dot = name.lastIndexOf(".");
  return dot === -1 ? "" : name.slice(dot).toLowerCase();
}

/** Path of a media item or a bare path string. */
export function pathOf(input) {
  if (!input) return "";
  return typeof input === "string" ? input : String(input.path ?? "");
}

/** Name of a media item, falling back to the basename of its path. */
export function fileNameOf(input) {
  const item = typeof input === "string" ? null : input;
  if (item?.name) return String(item.name);
  const path = pathOf(input);
  const parts = path.split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

/** "video", "image", or "unknown" — the file extension decides when present. */
export function mediaKind(input) {
  const item = typeof input === "string" ? null : input;
  const extension = extensionOf(pathOf(input));
  if (extension) {
    if (VIDEO_EXTENSIONS.includes(extension)) return "video";
    if (IMAGE_EXTENSIONS.includes(extension)) return "image";
    // A declared type can never make a non-media file previewable (the engine
    // would refuse to serve it), so the extension wins here.
    return "unknown";
  }
  const declared = String(item?.type ?? "").toLowerCase();
  return declared === "video" || declared === "image" ? declared : "unknown";
}

/** True when the composer can render this file. */
export function isPreviewable(input) {
  return mediaKind(input) !== "unknown";
}

/** Engine route that streams the file with HTTP range support. */
export function mediaStreamUrl(path) {
  return `/api/media/stream?path=${encodeURIComponent(pathOf(path))}`;
}

/** Engine route that serves a generated, cached single-frame thumbnail. */
export function mediaThumbUrl(path, width = THUMBNAIL_WIDTH) {
  return `/api/media/thumb?path=${encodeURIComponent(pathOf(path))}&width=${encodeURIComponent(width)}`;
}

/**
 * What the preview should render for one item.
 *
 * - image → the thumbnail route, falling back to the stream route if no
 *   thumbnail could be generated (no ffmpeg, odd format).
 * - video → the stream route as a real `<video>` source (playable, seekable,
 *   `preload: "metadata"` so the webview does not pull the whole file), with
 *   the thumbnail as its poster.
 *
 * Returns null for anything the composer cannot preview.
 */
export function previewPlan(input, { width = THUMBNAIL_WIDTH } = {}) {
  const path = pathOf(input);
  const kind = mediaKind(input);
  if (!path || kind === "unknown") return null;

  const stream = mediaStreamUrl(path);
  const thumb = mediaThumbUrl(path, width);
  if (kind === "video") {
    return { kind, path, src: stream, poster: thumb, fallbackSrc: null, controls: true, preload: "metadata", label: "Video" };
  }
  return { kind, path, src: thumb, poster: null, fallbackSrc: stream, controls: false, preload: null, label: "Image" };
}

/** Turn a list of filesystem paths (native picker / OS drop) into media items. */
export function mediaItemsFromPaths(paths, { source = "dropped" } = {}) {
  const list = Array.isArray(paths) ? paths : [paths];
  const items = [];
  for (const raw of list) {
    const path = String(raw ?? "").trim();
    if (!path || !isPreviewable(path)) continue;
    items.push({ path, name: fileNameOf(path), type: mediaKind(path), size_bytes: null, source });
  }
  return items;
}

/**
 * Merge new media into the existing list, newest first, de-duplicated by
 * path. A dropped or picked file becomes the selection (see `pick`), which is
 * what makes the preview appear immediately.
 */
export function mergeMedia(existing, additions) {
  const merged = [];
  const seen = new Set();
  for (const item of [...additions, ...(Array.isArray(existing) ? existing : [])]) {
    const path = pathOf(item);
    if (!path || seen.has(path)) continue;
    seen.add(path);
    merged.push(typeof item === "string" ? { path, name: fileNameOf(path), type: mediaKind(path) } : item);
  }
  return merged;
}

/** Files rejected by a drop/pick, for an honest one-line message. */
export function unsupportedPaths(paths) {
  const list = Array.isArray(paths) ? paths : [paths];
  return list.map((raw) => String(raw ?? "").trim()).filter((path) => path && !isPreviewable(path));
}

/** "1:05" / "12:34" for a duration in seconds (blank when unknown). */
export function formatDuration(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return "";
  const whole = Math.floor(seconds);
  const minutes = Math.floor(whole / 60);
  const rest = whole % 60;
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}

/** Short description shown under the preview: kind, size, duration, size. */
export function previewCaption(item, { duration = null, dimensions = null } = {}) {
  const kind = mediaKind(item);
  if (kind === "unknown") return "Not previewable";
  const bits = [kind === "video" ? "Video" : "Image"];
  const size = Number(item?.size_bytes);
  if (Number.isFinite(size) && size > 0) bits.push(formatBytes(size));
  const time = formatDuration(duration);
  if (time) bits.push(time);
  if (dimensions) bits.push(dimensions);
  return bits.join(" · ");
}
