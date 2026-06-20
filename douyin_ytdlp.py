#!/usr/bin/env python3
import csv
import datetime
import json
import sys
from pathlib import Path

from downloader import parse_urls_text, safe_name


WORKSPACE = Path(__file__).resolve().parent
DEPS_DIR = WORKSPACE / ".deps"
if str(DEPS_DIR) not in sys.path:
    sys.path.insert(0, str(DEPS_DIR))

from yt_dlp import YoutubeDL
from yt_dlp.cookies import SUPPORTED_BROWSERS
from yt_dlp.utils import DownloadError
import imageio_ffmpeg


MANIFEST_FIELDS = [
    "url",
    "status",
    "path",
    "id",
    "publish_time",
    "title",
    "ext",
    "extractor",
    "note",
]
VIDEO_META_SUFFIX = ".video.json"


def runtime_info():
    return {
        "yt_dlp_version": __import__("yt_dlp").version.__version__,
        "ffmpeg_path": imageio_ffmpeg.get_ffmpeg_exe(),
        "supported_browsers": sorted(SUPPORTED_BROWSERS),
    }


def parse_share_text(text):
    return parse_urls_text(text)


def normalize_browser(value):
    name = (value or "").strip().lower()
    if not name or name == "none":
        return None
    if name not in SUPPORTED_BROWSERS:
        raise ValueError(
            f"不支持的浏览器：{name}。可选值：{', '.join(sorted(SUPPORTED_BROWSERS))}"
        )
    return name


def write_manifest(path, rows):
    with Path(path).open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in MANIFEST_FIELDS})


def summarize_rows(rows):
    counts = {"ok": 0, "failed": 0, "skipped": 0}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return counts


def _cookies_from_browser(browser_name, profile):
    browser_name = normalize_browser(browser_name)
    if not browser_name:
        return None
    browser_profile = (profile or "").strip() or None
    return (browser_name, browser_profile, None, None)


def _emit(callback, event, index, total, payload):
    if callback:
        callback(event, index, total, payload)


def _guess_output_path(info, fallback_path=""):
    if not isinstance(info, dict):
        return fallback_path

    for item in info.get("requested_downloads") or []:
        if isinstance(item, dict):
            for key in ("filepath", "_filename"):
                if item.get(key):
                    return item[key]

    for key in ("filepath", "_filename", "filename"):
        if info.get(key):
            return info[key]

    return fallback_path


def _format_publish_time(value):
    if value in (None, ""):
        return ""
    try:
        return datetime.datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d_%H-%M-%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _build_video_name(info):
    publish_time = _format_publish_time(info.get("timestamp") or info.get("release_timestamp"))
    title = safe_name(str(info.get("title") or "").strip())[:80]
    aweme_id = str(info.get("id") or "").strip()
    ext = str(info.get("ext") or "mp4").strip() or "mp4"

    parts = []
    if publish_time:
        parts.append(publish_time)
    if title and title != "file":
        parts.append(title)
    if not parts:
        parts.append(aweme_id or "video")
    return f"{'_'.join(parts)}.{ext}", publish_time


def _resolve_path_conflict(target, overwrite):
    target = Path(target)
    if not target.exists():
        return target
    if overwrite:
        target.unlink()
        return target
    return target


def _flatten_info(info):
    if isinstance(info, dict) and info.get("_type") == "playlist":
        entries = [entry for entry in (info.get("entries") or []) if entry]
        return entries[0] if entries else info
    return info or {}


def _friendly_error_message(message):
    text = str(message or "").strip()
    if "failed to load cookies" in text or "Operation not permitted" in text:
        return (
            "读取浏览器 Cookie 失败。macOS 可能拦住了浏览器 Cookie 文件访问。"
            "你可以先把“浏览器 Cookie 来源”改成“不读取浏览器 Cookie”再试；"
            "如果必须用登录态，再考虑给当前终端或应用补系统权限。"
        )
    lowered = text.lower()
    if any(token in lowered for token in ("unsupported url", "no video formats", "extractor", "not found", "404")):
        return f"视频下载失败：这条链接可能不是可直接下载的视频页，或者作品已失效。{text}"
    if any(token in lowered for token in ("403", "401", "cookie", "login", "forbidden")):
        return f"视频下载失败：可能需要已登录浏览器 Cookie。{text}"
    return text


def _video_meta_path(media_path):
    path = Path(media_path)
    return path.with_suffix(path.suffix + VIDEO_META_SUFFIX)


def _write_video_meta(row):
    media_path = str(row.get("path") or "").strip()
    if not media_path:
        return
    meta = {
        "aweme_id": row.get("id", ""),
        "publish_time": row.get("publish_time", ""),
        "title": row.get("title", ""),
        "path": media_path,
        "ext": row.get("ext", ""),
    }
    _video_meta_path(media_path).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _yt_logger(index, total, callback, state):
    class Logger:
        def debug(self, msg):
            text = (msg or "").strip()
            if not text or text.startswith("[debug]"):
                return
            state["last_message"] = text
            _emit(callback, "log", index, total, {"message": text})

        def warning(self, msg):
            text = f"警告: {(msg or '').strip()}"
            state["last_message"] = text
            _emit(callback, "log", index, total, {"message": text})

        def error(self, msg):
            text = f"错误: {(msg or '').strip()}"
            state["last_message"] = text
            _emit(callback, "log", index, total, {"message": text})

    return Logger()


def run_share_single(
    url,
    output_dir,
    browser_name,
    browser_profile,
    retries,
    timeout,
    overwrite,
    progress_callback,
    index,
    total,
    *,
    emit_start=True,
):
    if emit_start:
        _emit(progress_callback, "start", index, total, {"url": url})
    state = {"last_message": "", "final_path": ""}

    def hook(status):
        kind = status.get("status")
        if kind == "downloading":
            downloaded = status.get("downloaded_bytes") or 0
            total_bytes = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
            if total_bytes:
                percent = downloaded / total_bytes * 100
                message = f"下载中 {percent:.1f}%"
            else:
                message = "下载中"
            _emit(progress_callback, "item_progress", index, total, {"message": message})
        elif kind == "finished":
            state["final_path"] = status.get("filename") or state["final_path"]
            _emit(progress_callback, "log", index, total, {"message": "文件下载完成，正在整理输出..."})

    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    cookie_source = _cookies_from_browser(browser_name, browser_profile)
    opts = {
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "paths": {"home": output_dir},
        "outtmpl": {"default": "%(id)s_%(title).120B.%(ext)s"},
        "ffmpeg_location": ffmpeg_path,
        "retries": retries,
        "fragment_retries": retries,
        "socket_timeout": timeout,
        "overwrites": overwrite,
        "noprogress": True,
        "quiet": True,
        "no_warnings": False,
        "logger": _yt_logger(index, total, progress_callback, state),
        "progress_hooks": [hook],
        "restrictfilenames": False,
    }
    if cookie_source:
        opts["cookiesfrombrowser"] = cookie_source

    try:
        with YoutubeDL(opts) as ydl:
            preflight = _flatten_info(ydl.extract_info(url, download=False))
            filename, publish_time = _build_video_name(preflight)
            target_path = Path(output_dir) / filename
            if target_path.exists() and not overwrite:
                row = {
                    "url": url,
                    "status": "skipped",
                    "path": str(target_path),
                    "id": preflight.get("id", ""),
                    "publish_time": publish_time,
                    "title": preflight.get("title", ""),
                    "ext": preflight.get("ext", ""),
                    "extractor": preflight.get("extractor_key") or preflight.get("extractor") or "",
                    "note": "file exists",
                }
                _emit(progress_callback, "finish", index, total, row)
                return row

            info = ydl.extract_info(url, download=True)
            flat = _flatten_info(info)
            source_path = Path(_guess_output_path(flat, state["final_path"]))
            final_path = target_path
            if source_path.exists() and source_path != final_path:
                final_path.parent.mkdir(parents=True, exist_ok=True)
                if final_path.exists() and overwrite:
                    final_path.unlink()
                source_path.replace(final_path)
            row = {
                "url": url,
                "status": "ok",
                "path": str(final_path if final_path.exists() else source_path),
                "id": flat.get("id", ""),
                "publish_time": publish_time or _format_publish_time(flat.get("timestamp") or flat.get("release_timestamp")),
                "title": flat.get("title", ""),
                "ext": flat.get("ext", ""),
                "extractor": flat.get("extractor_key") or flat.get("extractor") or "",
                "note": "",
            }
            if row["ext"] in {"mp3", "m4a"}:
                row["note"] = "当前提取结果是音频；图文/图集通常不会导出整组图片。"
            _write_video_meta(row)
            _emit(progress_callback, "finish", index, total, row)
            return row
    except DownloadError as exc:
        row = {
            "url": url,
            "status": "failed",
            "path": "",
            "id": "",
            "title": "",
            "ext": "",
            "extractor": "",
            "note": _friendly_error_message(exc),
        }
        _emit(progress_callback, "finish", index, total, row)
        return row
    except Exception as exc:  # pragma: no cover - surfaced in UI
        row = {
            "url": url,
            "status": "failed",
            "path": "",
            "id": "",
            "title": "",
            "ext": "",
            "extractor": "",
            "note": _friendly_error_message(exc),
        }
        _emit(progress_callback, "finish", index, total, row)
        return row


def run_share_batch(
    urls,
    output_dir="downloads",
    manifest_path="manifest.csv",
    browser_name="safari",
    browser_profile=None,
    retries=3,
    timeout=60,
    overwrite=False,
    progress_callback=None,
):
    urls = [url for url in urls if url]
    if not urls:
        raise ValueError("请先粘贴抖音分享文案或链接，一行一个。")

    output_dir = str(Path(output_dir))
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    cookie_source = _cookies_from_browser(browser_name, browser_profile)

    rows = []
    total = len(urls)

    for index, url in enumerate(urls, start=1):
        row = run_share_single(
            url=url,
            output_dir=output_dir,
            browser_name=browser_name,
            browser_profile=browser_profile,
            retries=retries,
            timeout=timeout,
            overwrite=overwrite,
            progress_callback=progress_callback,
            index=index,
            total=total,
        )
        rows.append(row)

    write_manifest(manifest_path, rows)
    summary = summarize_rows(rows)
    _emit(progress_callback, "summary", total, total, summary)
    return rows, summary


def pretty_runtime_info():
    return json.dumps(runtime_info(), ensure_ascii=False, indent=2)
