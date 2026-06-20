#!/usr/bin/env python3
import collections
import csv
import logging
import sys
from pathlib import Path
from urllib.parse import urlparse

from downloader import parse_urls_text


WORKSPACE = Path(__file__).resolve().parent
DEPS_DIR = WORKSPACE / ".deps"
if str(DEPS_DIR) not in sys.path:
    sys.path.insert(0, str(DEPS_DIR))

from gallery_dl import config as gallery_config
from gallery_dl import job as gallery_job
from gallery_dl import output as gallery_output
from gallery_dl import version as gallery_version
from gallery_dl.cookies import SUPPORTED_BROWSERS


MANIFEST_FIELDS = [
    "url",
    "status",
    "path",
    "id",
    "title",
    "ext",
    "extractor",
    "note",
    "count",
    "paths",
]

TIKTOK_HOSTS = {
    "www.tiktok.com",
    "m.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
    "tiktok.com",
}

DOUYIN_HOSTS = {
    "v.douyin.com",
    "www.douyin.com",
    "douyin.com",
    "iesdouyin.com",
}

_LOGGING_READY = False


def runtime_info():
    return {
        "gallery_dl_version": gallery_version.__version__,
        "supported_browsers": sorted(SUPPORTED_BROWSERS),
        "supports_douyin_image_share": False,
        "supports_tiktok_photo_post": True,
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
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in MANIFEST_FIELDS})


def summarize_rows(rows):
    counts = {"ok": 0, "failed": 0, "skipped": 0}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return counts


def _ensure_gallery_logging():
    global _LOGGING_READY
    if _LOGGING_READY:
        return
    gallery_output.initialize_logging(logging.WARNING)
    _LOGGING_READY = True


def _emit(callback, event, index, total, payload):
    if callback:
        callback(event, index, total, payload)


def _host_matches(host, allowed_hosts):
    if host in allowed_hosts:
        return True
    return any(host.endswith("." + item) for item in allowed_hosts)


def _url_host(url):
    return (urlparse(url).netloc or "").lower()


def _is_douyin_url(url):
    return _host_matches(_url_host(url), DOUYIN_HOSTS)


def _is_tiktok_url(url):
    return _host_matches(_url_host(url), TIKTOK_HOSTS)


def _cookies_from_browser(browser_name, profile):
    browser_name = normalize_browser(browser_name)
    if not browser_name:
        return None
    browser_profile = (profile or "").strip() or None
    return (browser_name, browser_profile, None, None, None)


def _set_common_runtime(output_dir, retries, timeout, overwrite, cookie_source):
    gallery_config.set((), "base-directory", str(Path(output_dir)))
    gallery_config.set((), "directory", ())
    gallery_config.set((), "retries", retries)
    gallery_config.set((), "timeout", timeout)
    gallery_config.set(("output",), "mode", "null")
    gallery_config.set(("extractor", "tiktok"), "photos", True)
    gallery_config.set(("extractor", "tiktok"), "audio", False)
    gallery_config.set(("extractor", "tiktok"), "videos", False)
    gallery_config.set(("extractor", "tiktok"), "covers", False)
    gallery_config.set(("extractor", "tiktok"), "subtitles", False)
    gallery_config.set(("extractor", "tiktok"), "postprocess", True)
    gallery_config.set(("extractor", "tiktok"), "skip", not overwrite)
    if cookie_source:
        gallery_config.set((), "cookies", cookie_source)
    else:
        gallery_config.unset((), "cookies")


def _new_row(url):
    return {
        "url": url,
        "status": "failed",
        "path": "",
        "id": "",
        "title": "",
        "ext": "",
        "extractor": "tiktok",
        "note": "",
        "count": 0,
        "paths": "",
    }


def _fill_row_from_pathfmt(row, pathfmt):
    kwdict = getattr(pathfmt, "kwdict", {}) or {}
    if not row["id"]:
        row["id"] = str(kwdict.get("id", "") or "")
    if not row["title"]:
        row["title"] = str(kwdict.get("title", "") or "")
    if not row["ext"]:
        row["ext"] = str(kwdict.get("extension", "") or "")
    row["extractor"] = "tiktok"


def _run_single_url(
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
):
    row = _new_row(url)
    _emit(progress_callback, "start", index, total, {"url": url})

    if _is_douyin_url(url):
        row["note"] = (
            "gallery-dl 当前上游只支持 TikTok 图文链接，"
            "不支持抖音分享页 / v.douyin.com 这类链接。"
        )
        _emit(progress_callback, "finish", index, total, row)
        return row

    if not _is_tiktok_url(url):
        row["note"] = "图文模式当前只接 TikTok 图文链接。"
        _emit(progress_callback, "finish", index, total, row)
        return row

    cookie_source = _cookies_from_browser(browser_name, browser_profile)
    _set_common_runtime(output_dir, retries, timeout, overwrite, cookie_source)
    _ensure_gallery_logging()

    downloaded_paths = []
    skipped_paths = []
    error_paths = []

    def on_prepare(pathfmt):
        _fill_row_from_pathfmt(row, pathfmt)
        preview_path = getattr(pathfmt, "path", "") or ""
        if preview_path:
            _emit(
                progress_callback,
                "item_progress",
                index,
                total,
                {"message": f"准备写入: {preview_path}"},
            )

    def on_after(pathfmt):
        _fill_row_from_pathfmt(row, pathfmt)
        final_path = getattr(pathfmt, "path", "") or ""
        if final_path:
            downloaded_paths.append(final_path)
            _emit(progress_callback, "log", index, total, {"message": f"已下载: {final_path}"})

    def on_skip(pathfmt):
        _fill_row_from_pathfmt(row, pathfmt)
        final_path = getattr(pathfmt, "path", "") or ""
        if final_path:
            skipped_paths.append(final_path)
            _emit(progress_callback, "log", index, total, {"message": f"已跳过: {final_path}"})

    def on_error(pathfmt):
        _fill_row_from_pathfmt(row, pathfmt)
        final_path = getattr(pathfmt, "path", "") or ""
        if final_path:
            error_paths.append(final_path)
            _emit(progress_callback, "log", index, total, {"message": f"下载失败: {final_path}"})

    try:
        job = gallery_job.DownloadJob(url)
        job.hooks = collections.defaultdict(list)
        job.register_hooks(
            {
                "prepare-after": on_prepare,
                "after": on_after,
                "skip": on_skip,
                "error": on_error,
            }
        )
        status = job.run()
    except Exception as exc:
        row["note"] = str(exc) or "gallery-dl 执行失败。"
        _emit(progress_callback, "finish", index, total, row)
        return row

    unique_paths = []
    seen = set()
    for path in downloaded_paths + skipped_paths:
        if path and path not in seen:
            unique_paths.append(path)
            seen.add(path)

    row["count"] = len(unique_paths)
    row["path"] = unique_paths[0] if unique_paths else ""
    row["paths"] = "\n".join(unique_paths)

    if downloaded_paths:
        row["status"] = "ok"
        row["note"] = f"已导出 {len(downloaded_paths)} 个文件。"
    elif skipped_paths and not error_paths and status == 0:
        row["status"] = "skipped"
        row["note"] = f"全部已存在，共跳过 {len(skipped_paths)} 个文件。"
    else:
        row["status"] = "failed"
        row["note"] = (
            row["note"]
            or "没有提取到图片。这个链接可能不是 TikTok 图文帖子，或当前需要登录态。"
        )

    _emit(progress_callback, "finish", index, total, row)
    return row


def run_image_batch(
    urls,
    output_dir="downloads",
    manifest_path="manifest.csv",
    browser_name="none",
    browser_profile=None,
    retries=3,
    timeout=60,
    overwrite=False,
    progress_callback=None,
):
    urls = [url for url in urls if url]
    if not urls:
        raise ValueError("请先粘贴分享文案或链接，一行一个。")

    output_dir = str(Path(output_dir))
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    rows = []
    total = len(urls)
    for index, url in enumerate(urls, start=1):
        row = _run_single_url(
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
