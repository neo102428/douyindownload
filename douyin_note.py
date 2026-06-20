#!/usr/bin/env python3
import csv
import hashlib
import json
import mimetypes
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from downloader import CHUNK_SIZE, DEFAULT_UA, extract_url, parse_urls_text, safe_name


WORKSPACE = Path(__file__).resolve().parent
DEPS_DIR = WORKSPACE / ".deps"
if str(DEPS_DIR) not in sys.path:
    sys.path.insert(0, str(DEPS_DIR))

from yt_dlp.cookies import SUPPORTED_BROWSERS, extract_cookies_from_browser
import imageio_ffmpeg


DOUYIN_HOSTS = {
    "v.douyin.com",
    "www.douyin.com",
    "douyin.com",
    "www.iesdouyin.com",
    "iesdouyin.com",
}

DETAIL_URL = "https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id={aweme_id}"
DETAIL_URL_WEBAPP = (
    "https://www.douyin.com/aweme/v1/web/aweme/detail/"
    "?aweme_id={aweme_id}"
    "&aid=6383"
    "&device_platform=webapp"
    "&browser_language=zh-CN"
    "&browser_platform=MacIntel"
    "&browser_name=Chrome"
    "&browser_version=137.0.0.0"
)
MANIFEST_FIELDS = [
    "url",
    "status",
    "path",
    "dir",
    "id",
    "title",
    "kind",
    "image_count",
    "motion_count",
    "audio_path",
    "video_path",
    "note",
]
IMAGE_FIELDS = ("original_images", "images", "image_list", "image_infos")
MOTION_HINTS = (
    "motion",
    "live",
    "video",
    "play",
    "dynamic",
    "clip",
    "anim",
)
IMAGE_HINTS = (
    "image",
    "images",
    "photo",
    "origin",
    "original",
    "download",
    "display",
)
EXTENSION_MAP = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
}


def runtime_info():
    return {
        "ffmpeg_path": imageio_ffmpeg.get_ffmpeg_exe(),
        "supported_browsers": sorted(SUPPORTED_BROWSERS),
        "detail_endpoint": "aweme/v1/web/aweme/detail",
        "detail_endpoint_fallback": "aweme/v1/web/aweme/detail + webapp params",
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


def _emit(callback, event, index, total, payload):
    if callback:
        callback(event, index, total, payload)


def _friendly_error_message(message):
    text = str(message or "").strip()
    if "failed to load cookies" in text or "Operation not permitted" in text:
        return (
            "读取浏览器 Cookie 失败。macOS 可能拦住了浏览器 Cookie 文件访问。"
            "你可以先改成“不读取浏览器 Cookie”再试；"
            "如果必须用登录态，再考虑给当前终端或应用补系统权限。"
        )
    return text


def _build_opener(browser_name, profile):
    browser_name = normalize_browser(browser_name)
    handlers = []
    if browser_name:
        browser_profile = (profile or "").strip() or None
        cookie_jar = extract_cookies_from_browser(browser_name, browser_profile)
        handlers.append(urllib.request.HTTPCookieProcessor(cookie_jar))
    return urllib.request.build_opener(*handlers)


def _default_headers(referer=None, accept=None):
    headers = {
        "User-Agent": DEFAULT_UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    if accept:
        headers["Accept"] = accept
    return headers


def _open(opener, url, timeout, *, method="GET", headers=None):
    request = urllib.request.Request(url, headers=headers or {}, method=method)
    return opener.open(request, timeout=timeout)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _extract_redirect_location(url, timeout):
    opener = urllib.request.build_opener(_NoRedirectHandler())
    headers = _default_headers(referer="https://www.douyin.com/")
    for method in ("HEAD", "GET"):
        try:
            with _open(opener, url, timeout, method=method, headers=headers) as response:
                location = response.headers.get("Location") or response.geturl()
                if location:
                    return location
        except urllib.error.HTTPError as exc:
            if exc.code in {301, 302, 303, 307, 308}:
                location = exc.headers.get("Location")
                if location:
                    return location
            if method == "HEAD" and exc.code in {403, 405}:
                continue
            raise
    return url


def _resolve_final_url(opener, url, timeout):
    direct = _extract_aweme_id(url)
    if direct:
        return url, direct[0], direct[1]

    parsed = urllib.parse.urlparse(url)
    if (parsed.netloc or "").lower() == "v.douyin.com":
        redirected = _extract_redirect_location(url, timeout)
        direct = _extract_aweme_id(redirected)
        if direct:
            return redirected, direct[0], direct[1]

    headers = _default_headers(referer="https://www.douyin.com/")
    for method in ("HEAD", "GET"):
        try:
            with _open(opener, url, timeout, method=method, headers=headers) as response:
                final_url = response.geturl() or url
                direct = _extract_aweme_id(final_url)
                if direct:
                    return final_url, direct[0], direct[1]
        except urllib.error.HTTPError as exc:
            if method == "HEAD" and exc.code in {403, 405}:
                continue
            raise

    value = extract_url(url)
    raise ValueError(f"没能从分享链接里解析出作品 ID：{value}")


def _extract_aweme_id(url):
    parsed = urllib.parse.urlparse(url)
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""
    if host not in DOUYIN_HOSTS and not host.endswith(".douyin.com"):
        return None

    patterns = (
        (r"/(?:video|note|slides)/(?P<id>[0-9]{8,24})", "direct"),
        (r"/share/(?:video|note|slides)/(?P<id>[0-9]{8,24})", "share"),
    )
    for pattern, kind in patterns:
        if match := re.search(pattern, path):
            return match.group("id"), kind

    query = urllib.parse.parse_qs(parsed.query)
    for key in ("aweme_id", "modal_id", "group_id", "item_id"):
        value = (query.get(key) or [None])[0]
        if value and value.isdigit():
            return value, "query"
    return None


def _fetch_detail(opener, aweme_id, timeout):
    headers = _default_headers(
        referer=f"https://www.douyin.com/video/{aweme_id}",
        accept="application/json, text/plain, */*",
    )
    candidate_urls = [
        DETAIL_URL.format(aweme_id=aweme_id),
        DETAIL_URL_WEBAPP.format(aweme_id=aweme_id),
    ]
    last_payload = None
    for url in candidate_urls:
        with _open(opener, url, timeout, headers=headers) as response:
            payload = json.loads(response.read().decode("utf-8"))
        last_payload = payload
        detail = payload.get("aweme_detail")
        if isinstance(detail, dict):
            return detail

    filter_detail = (last_payload or {}).get("filter_detail") or {}
    reason = str(filter_detail.get("filter_reason") or "").strip()
    if reason:
        raise ValueError(f"抖音详情接口没有返回 aweme_detail，平台返回原因：{reason}")
    raise ValueError("抖音详情接口没有返回 aweme_detail，可能需要更新 Cookie 或链接已失效。")


def _detail_title(detail, aweme_id):
    title = (
        detail.get("desc")
        or detail.get("preview_title")
        or detail.get("item_title")
        or f"douyin_{aweme_id}"
    )
    return title.strip() or f"douyin_{aweme_id}"


def _detail_kind(detail):
    aweme_type = detail.get("aweme_type")
    if any(detail.get(name) for name in IMAGE_FIELDS):
        return f"image_aweme_type_{aweme_type}"
    if detail.get("video"):
        return f"video_aweme_type_{aweme_type}"
    return f"unknown_aweme_type_{aweme_type}"


def _flatten_entries(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, (dict, list, str))]
    if isinstance(value, dict):
        if any(key in value for key in ("url_list", "download_url_list", "uri")):
            return [value]
        flat = []
        for item in value.values():
            flat.extend(_flatten_entries(item))
        return flat
    return [value] if isinstance(value, str) else []


def _entry_identity(entry):
    if isinstance(entry, dict):
        for key in ("uri", "image_id", "photo_id", "id"):
            value = entry.get(key)
            if value:
                return str(value)
    text = json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _collect_image_entries(detail):
    entries = []
    seen = set()

    top_level_keys = list(IMAGE_FIELDS)
    for key in detail.keys():
        lowered = key.lower()
        if key in IMAGE_FIELDS:
            continue
        if lowered.endswith(("_images", "_image_list", "_image_infos")) or lowered in {
            "photo_list",
        }:
            top_level_keys.append(key)
    for key in top_level_keys:
        for entry in _flatten_entries(detail.get(key)):
            marker = _entry_identity(entry)
            if marker in seen:
                continue
            seen.add(marker)
            entries.append(entry)
    return entries


def _is_url(value):
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def _collect_url_groups(value, trail=()):
    groups = []
    if isinstance(value, dict):
        for key, item in value.items():
            groups.extend(_collect_url_groups(item, trail + (str(key),)))
    elif isinstance(value, list):
        if value and all(_is_url(item) for item in value):
            groups.append((trail, list(value)))
        else:
            for item in value:
                groups.extend(_collect_url_groups(item, trail + ("[]",)))
    elif _is_url(value):
        groups.append((trail, [value]))
    return groups


def _score_group(path_text, urls, *, motion):
    score = 0
    if motion:
        if any(hint in path_text for hint in MOTION_HINTS):
            score += 120
        if any(url.split("?", 1)[0].lower().endswith((".mp4", ".mov", ".webm", ".mkv")) for url in urls):
            score += 80
        if any(hint in path_text for hint in IMAGE_HINTS):
            score -= 60
    else:
        if any(hint in path_text for hint in IMAGE_HINTS):
            score += 90
        if "url_list" in path_text:
            score += 20
        if any(hint in path_text for hint in ("origin", "original")):
            score += 40
        if any(hint in path_text for hint in MOTION_HINTS):
            score -= 220
    return score


def _rank_entry_urls(entry, *, motion):
    candidates = []
    for trail, urls in _collect_url_groups(entry):
        path_text = ".".join(trail).lower()
        score = _score_group(path_text, urls, motion=motion)
        if motion and score <= 0:
            continue
        if not motion and score <= 0:
            continue
        unique_urls = []
        seen = set()
        for url in urls:
            if url not in seen:
                unique_urls.append(url)
                seen.add(url)
        candidates.append((score, path_text, unique_urls))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in candidates]


def _extract_audio_urls(detail):
    music = detail.get("music") or {}
    play_url = music.get("play_url") or {}
    candidates = []
    for value in (play_url.get("url_list") or []):
        if _is_url(value):
            candidates.append(value)
    if _is_url(music.get("playUrl")):
        candidates.append(music["playUrl"])
    return list(dict.fromkeys(candidates))


def _guess_extension(url, headers, default=".bin"):
    content_type = (headers.get("Content-Type", "") or "").split(";", 1)[0].strip().lower()
    if content_type in EXTENSION_MAP:
        return EXTENSION_MAP[content_type]
    ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if ext:
        return ext
    guess = mimetypes.guess_extension(content_type) if content_type else None
    if guess == ".jpe":
        return ".jpg"
    return guess or default


def _concat_escape(path):
    return str(Path(path).resolve()).replace("'", "'\\''")


def _download_best_candidate(
    opener,
    url_groups,
    target_stem,
    timeout,
    retries,
    overwrite,
    referer,
):
    if not url_groups:
        return None

    errors = []
    for group in url_groups:
        for url in group:
            tmp_path = None
            for attempt in range(1, retries + 1):
                try:
                    with _open(opener, url, timeout, headers=_default_headers(referer=referer)) as response:
                        meta = dict(response.info().items())
                        ext = _guess_extension(url, meta)
                        target = target_stem.with_suffix(ext)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if target.exists() and not overwrite:
                            return {
                                "status": "skipped",
                                "path": str(target),
                                "content_type": meta.get("Content-Type", ""),
                            }

                        tmp_path = target.with_suffix(target.suffix + ".part")
                        with tmp_path.open("wb") as fh:
                            while True:
                                chunk = response.read(CHUNK_SIZE)
                                if not chunk:
                                    break
                                fh.write(chunk)
                        tmp_path.replace(target)
                        return {
                            "status": "ok",
                            "path": str(target),
                            "content_type": meta.get("Content-Type", ""),
                        }
                except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
                    errors.append(str(exc))
                    if tmp_path and tmp_path.exists():
                        tmp_path.unlink()
                    if attempt < retries:
                        time.sleep(min(2 * attempt, 5))
    raise ValueError(errors[-1] if errors else "资源下载失败")


def _transcode_single_to_mp4(source_path, output_path, ffmpeg_path):
    command = [
        ffmpeg_path,
        "-y",
        "-i",
        str(source_path),
        "-c:v",
        "libx264",
        "-movflags",
        "+faststart",
        "-pix_fmt",
        "yuv420p",
        str(output_path),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)
    return output_path


def _merge_motion_files(motion_paths, output_path, ffmpeg_path):
    normalized_paths = []
    for index, path in enumerate(motion_paths, start=1):
        path = Path(path)
        normalized = output_path.parent / f"_motion_normalized_{index:02d}.mp4"
        _transcode_single_to_mp4(path, normalized, ffmpeg_path)
        normalized_paths.append(normalized)

    concat_path = output_path.parent / "_motion_concat.txt"
    lines = []
    for path in normalized_paths:
        lines.append(f"file '{_concat_escape(path)}'")
    concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        subprocess.run(
            [
                ffmpeg_path,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_path),
                "-c:v",
                "libx264",
                "-movflags",
                "+faststart",
                "-pix_fmt",
                "yuv420p",
                str(output_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        concat_path.unlink(missing_ok=True)
        for path in normalized_paths:
            if path.name.startswith("_motion_normalized_"):
                path.unlink(missing_ok=True)
    return output_path


def _build_slideshow(image_paths, output_path, ffmpeg_path, audio_path=None):
    concat_path = output_path.parent / "_slideshow_concat.txt"
    lines = []
    for path in image_paths:
        path_text = _concat_escape(path)
        lines.append(f"file '{path_text}'")
        lines.append("duration 1.2")
    if image_paths:
        last_path = _concat_escape(image_paths[-1])
        lines.append(f"file '{last_path}'")
    concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        command = [
            ffmpeg_path,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_path),
        ]
        if audio_path:
            command.extend(["-i", str(audio_path), "-shortest"])
        command.extend(
            [
                "-c:v",
                "libx264",
                "-vsync",
                "vfr",
                "-movflags",
                "+faststart",
                "-pix_fmt",
                "yuv420p",
                str(output_path),
            ]
        )
        subprocess.run(command, check=True, capture_output=True, text=True)
    finally:
        concat_path.unlink(missing_ok=True)
    return output_path


def _make_output_dir(output_dir, aweme_id, title):
    folder_name = f"{aweme_id}_{safe_name(title)[:80]}"
    target = Path(output_dir) / folder_name
    target.mkdir(parents=True, exist_ok=True)
    return target


def _new_row(url):
    return {
        "url": url,
        "status": "failed",
        "path": "",
        "dir": "",
        "id": "",
        "title": "",
        "kind": "",
        "image_count": 0,
        "motion_count": 0,
        "audio_path": "",
        "video_path": "",
        "note": "",
    }


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
    _emit(progress_callback, "item_progress", index, total, {"message": "正在解析分享链接"})

    opener = _build_opener(browser_name, browser_profile)
    final_url, aweme_id, _link_kind = _resolve_final_url(opener, url, timeout)
    row["id"] = aweme_id

    _emit(progress_callback, "item_progress", index, total, {"message": "正在读取作品详情"})
    detail = _fetch_detail(opener, aweme_id, timeout)
    row["title"] = _detail_title(detail, aweme_id)
    row["kind"] = _detail_kind(detail)

    image_entries = _collect_image_entries(detail)
    if not image_entries:
        row["status"] = "skipped"
        row["note"] = "当前链接解析结果是视频作品，或暂未发现图文资源。"
        _emit(progress_callback, "finish", index, total, row)
        return row

    item_dir = _make_output_dir(output_dir, aweme_id, row["title"])
    row["dir"] = str(item_dir)

    image_paths = []
    motion_paths = []

    for item_index, entry in enumerate(image_entries, start=1):
        still_groups = _rank_entry_urls(entry, motion=False)
        if still_groups:
            result = _download_best_candidate(
                opener,
                still_groups,
                item_dir / "images" / f"image_{item_index:02d}",
                timeout,
                retries,
                overwrite,
                referer=final_url,
            )
            if result:
                content_type = (result.get("content_type", "") or "").split(";", 1)[0].lower()
                bucket = motion_paths if content_type.startswith("video/") else image_paths
                label = "动图资源已保存" if bucket is motion_paths else "图片已保存"
                if result["path"] not in bucket:
                    bucket.append(result["path"])
                    _emit(progress_callback, "log", index, total, {"message": f"{label}: {result['path']}"})

        motion_groups = _rank_entry_urls(entry, motion=True)
        if motion_groups:
            result = _download_best_candidate(
                opener,
                motion_groups,
                item_dir / "motion" / f"motion_{item_index:02d}",
                timeout,
                retries,
                overwrite,
                referer=final_url,
            )
            if result:
                content_type = (result.get("content_type", "") or "").split(";", 1)[0].lower()
                bucket = motion_paths if content_type.startswith("video/") else image_paths
                label = "动图资源已保存" if bucket is motion_paths else "图片已保存"
                if result["path"] not in bucket:
                    bucket.append(result["path"])
                    _emit(progress_callback, "log", index, total, {"message": f"{label}: {result['path']}"})

    if not image_paths and not motion_paths:
        row["note"] = "接口里出现了图文字段，但没有提取到可下载的图片或动图资源。"
        _emit(progress_callback, "finish", index, total, row)
        return row

    audio_path = ""
    audio_urls = _extract_audio_urls(detail)
    if audio_urls:
        try:
            audio_result = _download_best_candidate(
                opener,
                [audio_urls],
                item_dir / "audio" / "bgm",
                timeout,
                retries,
                overwrite,
                referer=final_url,
            )
            audio_path = audio_result["path"]
            row["audio_path"] = audio_path
            _emit(progress_callback, "log", index, total, {"message": f"配乐已保存: {audio_path}"})
        except Exception:
            pass

    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    video_path = ""
    try:
        if len(motion_paths) == 1:
            source_path = Path(motion_paths[0])
            if source_path.suffix.lower() == ".mp4":
                video_path = str(source_path)
            else:
                target_path = item_dir / "preview_motion.mp4"
                video_path = str(_transcode_single_to_mp4(source_path, target_path, ffmpeg_path))
        elif len(motion_paths) > 1:
            target_path = item_dir / "preview_motion_merged.mp4"
            video_path = str(_merge_motion_files(motion_paths, target_path, ffmpeg_path))
        elif image_paths:
            target_path = item_dir / "preview_slideshow.mp4"
            video_path = str(_build_slideshow(image_paths, target_path, ffmpeg_path, audio_path or None))
        if video_path:
            row["video_path"] = video_path
            _emit(progress_callback, "log", index, total, {"message": f"预览视频已生成: {video_path}"})
    except Exception as exc:
        _emit(progress_callback, "log", index, total, {"message": f"生成 MP4 失败: {exc}"})

    row["image_count"] = len(image_paths)
    row["motion_count"] = len(motion_paths)
    row["path"] = row["video_path"] or (image_paths[0] if image_paths else motion_paths[0])
    row["status"] = "ok"
    row["note"] = (
        f"已导出 {len(image_paths)} 张图片，"
        f"{len(motion_paths)} 个动图资源。"
        + (" 已生成 MP4。" if video_path else "")
    )
    _emit(progress_callback, "finish", index, total, row)
    return row


def run_note_batch(
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
        raise ValueError("请先粘贴抖音分享文案或链接，一行一个。")

    output_dir = str(Path(output_dir))
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    rows = []
    total = len(urls)
    for index, url in enumerate(urls, start=1):
        try:
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
        except Exception as exc:
            row = _new_row(url)
            row["status"] = "failed"
            row["note"] = _friendly_error_message(exc)
            _emit(progress_callback, "finish", index, total, row)
        rows.append(row)

    write_manifest(manifest_path, rows)
    summary = summarize_rows(rows)
    _emit(progress_callback, "summary", total, total, summary)
    return rows, summary
