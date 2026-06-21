#!/usr/bin/env python3
import csv
import datetime
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from downloader import DEFAULT_UA, extract_url
from downloader import safe_name
from douyin_note import (
    _default_headers,
    _emit,
    _extract_redirect_location,
    _friendly_error_message as note_friendly_error_message,
    _open,
    _build_opener,
    _load_existing_index,
    _run_single_url as run_note_single,
)
from douyin_ytdlp import run_share_single


WORKSPACE = Path(__file__).resolve().parent
DEPS_DIR = WORKSPACE / ".deps"
if str(DEPS_DIR) not in sys.path:
    sys.path.insert(0, str(DEPS_DIR))


AUTHOR_POST_URL = (
    "https://www.douyin.com/aweme/v1/web/aweme/post/"
    "?sec_user_id={sec_user_id}"
    "&count={count}"
    "&max_cursor={max_cursor}"
    "&aid=6383"
    "&device_platform=webapp"
    "&browser_language=zh-CN"
    "&browser_platform=MacIntel"
    "&browser_name=Chrome"
    "&browser_version=137.0.0.0"
)
AUTHOR_MANIFEST_FIELDS = [
    "source",
    "author_sec_user_id",
    "author_nickname",
    "url",
    "aweme_id",
    "detected_mode",
    "status",
    "path",
    "dir",
    "publish_time",
    "title",
    "kind",
    "image_count",
    "motion_count",
    "audio_path",
    "video_path",
    "ext",
    "extractor",
    "note",
]
AUTHOR_RESUME_NAME = ".douyin_author_resume.json"


def runtime_info():
    return {
        "supports_author_batch": True,
        "author_source": "douyin aweme post webapp api",
        "author_browser_fallback": True,
    }


def write_manifest(path, rows):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=AUTHOR_MANIFEST_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in AUTHOR_MANIFEST_FIELDS})


def summarize_rows(rows):
    counts = {"ok": 0, "failed": 0, "skipped": 0}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return counts


def _author_request_headers(referer=None):
    return _default_headers(
        referer=referer or "https://www.douyin.com/",
        accept="application/json, text/plain, */*",
    )


def _extract_sec_user_id(url):
    url = extract_url(url)
    parsed = urllib.parse.urlparse(url)
    path = parsed.path or ""
    query = urllib.parse.parse_qs(parsed.query)

    value = (query.get("sec_user_id") or [None])[0]
    if value:
        return value

    match = re.search(r"/user/(?P<id>[^/?#]+)", path)
    if match:
        return match.group("id")
    return None


def _resolve_author_sec_user_id(opener, url, timeout):
    url = extract_url(url)
    direct = _extract_sec_user_id(url)
    if direct:
        return direct, url

    parsed = urllib.parse.urlparse(url)
    if (parsed.netloc or "").lower() == "v.douyin.com":
        redirected = _extract_redirect_location(url, timeout)
        direct = _extract_sec_user_id(redirected)
        if direct:
            return direct, redirected

    headers = _default_headers(referer="https://www.douyin.com/")
    for method in ("HEAD", "GET"):
        try:
            with _open(opener, url, timeout, method=method, headers=headers) as response:
                final_url = response.geturl() or url
                direct = _extract_sec_user_id(final_url)
                if direct:
                    return direct, final_url
        except urllib.error.HTTPError as exc:
            if method == "HEAD" and exc.code in {403, 405}:
                continue
            raise

    raise ValueError("没能从作者主页链接里解析出 sec_user_id。")


def _fetch_author_page(opener, sec_user_id, max_cursor, count, timeout):
    url = AUTHOR_POST_URL.format(
        sec_user_id=urllib.parse.quote(sec_user_id, safe=""),
        count=count,
        max_cursor=max_cursor,
    )
    with _open(opener, url, timeout, headers=_author_request_headers()) as response:
        raw = response.read().decode("utf-8")
    if not raw:
        raise ValueError("作者作品列表接口返回空响应，可能需要浏览器 Cookie。")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("作者作品列表接口返回异常。")
    return payload


def _collect_browser_aweme_ids(
    author_url,
    timeout,
    max_items,
    progress_callback,
):
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise RuntimeError(
            "作者主页浏览器回补需要先安装 Playwright。"
            "可执行：python3 -m pip install playwright && python3 -m playwright install chromium"
        ) from exc

    limit = int(max_items or 0)
    seen = set()
    collected = []
    timeout_ms = max(30, int(timeout)) * 1000

    def merge_ids(json_str):
        added = 0
        try:
            items = json.loads(json_str) if json_str else []
        except (json.JSONDecodeError, TypeError):
            return 0
        for item in items or []:
            aweme_id = str(item.get("id", "") if isinstance(item, dict) else item).strip()
            link_type = item.get("type", "video") if isinstance(item, dict) else "video"
            if not aweme_id or aweme_id in seen:
                continue
            seen.add(aweme_id)
            collected.append({"id": aweme_id, "type": link_type})
            added += 1
            if limit and len(collected) >= limit:
                break
        return added

    script = """
() => {
  const result = [];
  const seen = new Set();
  const push = (id, type) => {
    if (!id || seen.has(id)) return;
    seen.add(id);
    result.push({id: id, type: type});
  };
  const collectFrom = (text, pattern, type) => {
    if (!text) return;
    let match;
    while ((match = pattern.exec(text)) !== null) {
      push(match[1], type);
    }
  };
  for (const node of document.querySelectorAll('a[href]')) {
    const href = node.getAttribute('href') || '';
    collectFrom(href, /\\/video\\/(\\d{15,20})/g, 'video');
    collectFrom(href, /\\/note\\/(\\d{15,20})/g, 'note');
    collectFrom(href, /\\/slides\\/(\\d{15,20})/g, 'note');
  }
  const html = document.documentElement ? document.documentElement.innerHTML : '';
  collectFrom(html, /"aweme_id":"(\\d{15,20})"/g, 'video');
  collectFrom(html, /"group_id":"(\\d{15,20})"/g, 'video');
  return JSON.stringify(result);
}
"""

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        context = browser.new_context(locale="zh-CN", viewport={"width": 1600, "height": 900})
        page = context.new_page()
        try:
            _emit(progress_callback, "log", 0, limit or 0, {"message": "作者页接口可能漏作品，正在启动浏览器回补。"})
            page.goto(author_url, wait_until="domcontentloaded", timeout=timeout_ms)

            # 等待用户登录
            _emit(progress_callback, "log", 0, limit or 0, {"message": "浏览器已打开抖音页面，请完成登录（包括验证码）。"})
            _emit(progress_callback, "log", 0, limit or 0, {"message": "登录完成后，页面会显示作者的作品列表。"})
            _emit(progress_callback, "log", 0, limit or 0, {"message": "看到作品列表后，请在终端按回车键开始收集作品ID。"})

            # 简单等待用户按回车
            try:
                input(">>> 请在登录完成后按回车键继续...")
            except:
                pass

            _emit(progress_callback, "log", 0, limit or 0, {"message": "开始收集作品ID。"})

            last_count = 0
            stable_rounds = 0
            max_rounds = 240
            for round_index in range(1, max_rounds + 1):
                merge_ids(page.evaluate(script))
                if limit and len(collected) >= limit:
                    break
                if len(collected) == last_count:
                    stable_rounds += 1
                else:
                    stable_rounds = 0
                    last_count = len(collected)
                if stable_rounds >= 8:
                    break
                if round_index == 1 or round_index % 10 == 0:
                    _emit(
                        progress_callback,
                        "log",
                        0,
                        limit or 0,
                        {"message": f"浏览器回补中，已发现 {len(collected)} 条作品 ID。"},
                    )
                page.mouse.wheel(0, 3800)
                page.wait_for_timeout(1200)
        finally:
            context.close()
            browser.close()

    return collected


def _aweme_share_url(aweme):
    aweme_id = str(aweme.get("aweme_id") or "").strip()
    if not aweme_id:
        return ""
    if aweme.get("is_slides") or aweme.get("images") or aweme.get("image_infos") or aweme.get("slide_image_infos") or aweme.get("image_list"):
        return f"https://www.douyin.com/note/{aweme_id}"
    return f"https://www.douyin.com/video/{aweme_id}"


def _aweme_detected_mode(aweme):
    if (
        aweme.get("is_slides")
        or aweme.get("images")
        or aweme.get("image_infos")
        or aweme.get("slide_image_infos")
        or aweme.get("image_list")
        or aweme.get("image_infos_list")
        or aweme.get("image")
        or aweme.get("mix_images")
    ):
        return "douyin_media"
    return "video"


def _row_from_result(source, sec_user_id, nickname, detected_mode, result):
    row = {
        "source": source,
        "author_sec_user_id": sec_user_id,
        "author_nickname": nickname,
        "url": result.get("url", ""),
        "aweme_id": result.get("id", ""),
        "detected_mode": detected_mode,
        "status": result.get("status", "failed"),
        "path": result.get("path", ""),
        "dir": result.get("dir", ""),
        "publish_time": result.get("publish_time", ""),
        "title": result.get("title", ""),
        "kind": result.get("kind", ""),
        "image_count": result.get("image_count", 0),
        "motion_count": result.get("motion_count", 0),
        "audio_path": result.get("audio_path", ""),
        "video_path": result.get("video_path", ""),
        "ext": result.get("ext", ""),
        "extractor": result.get("extractor", ""),
        "note": result.get("note", ""),
    }
    return row


def _load_existing_aweme_index(output_dir):
    index = {}
    media_index = _load_existing_index(output_dir)
    for aweme_id, path in media_index.items():
        index.setdefault(aweme_id, Path(path))

    root = Path(output_dir)
    if not root.exists():
        return index

    for child in root.iterdir():
        if not child.is_file() or not child.name.endswith(".video.json"):
            continue
        try:
            meta = json.loads(child.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        aweme_id = str(meta.get("aweme_id") or "").strip()
        media_path = str(meta.get("path") or "").strip()
        if aweme_id and media_path:
            index.setdefault(aweme_id, Path(media_path))

    for child in root.iterdir():
        if not child.is_file():
            continue
        match = re.match(r"^(?P<id>[0-9]{8,24})_", child.name)
        if match:
            index.setdefault(match.group("id"), child)
    return index


def _rebuild_seen_from_downloads(output_dir):
    return set(str(aweme_id) for aweme_id in _load_existing_aweme_index(output_dir).keys())


def _format_publish_time(value):
    if value in (None, ""):
        return ""
    try:
        return datetime.datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d_%H-%M-%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _legacy_video_candidates(aweme):
    aweme_id = str(aweme.get("aweme_id") or "").strip()
    if not aweme_id:
        return []
    publish_time = _format_publish_time(aweme.get("create_time"))
    desc = safe_name(str(aweme.get("desc") or aweme.get("preview_title") or "").strip())[:80]
    candidates = []
    if publish_time and desc:
        candidates.append(f"{publish_time}_{desc}")
    if publish_time:
        candidates.append(f"{publish_time}_Douyin_video_{aweme_id}")
    candidates.append(f"{aweme_id}_")
    candidates.append(f"Douyin_video_{aweme_id}")
    return list(dict.fromkeys(candidates))


def _locate_existing_video_file(output_dir, aweme):
    root = Path(output_dir)
    if not root.exists():
        return None
    for prefix in _legacy_video_candidates(aweme):
        for candidate in root.glob(f"{prefix}*"):
            if candidate.is_file() and candidate.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm", ".m4v"}:
                return candidate
    return None


def _author_resume_path(output_dir):
    return Path(output_dir) / AUTHOR_RESUME_NAME


def _load_author_resume(output_dir, sec_user_id):
    path = _author_resume_path(output_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if str(data.get("sec_user_id") or "").strip() != str(sec_user_id).strip():
        return {}
    if not isinstance(data.get("seen_aweme_ids"), list):
        data["seen_aweme_ids"] = []
    if not isinstance(data.get("failed_aweme_ids"), list):
        data["failed_aweme_ids"] = []
    return data


def _save_author_resume(output_dir, data):
    path = _author_resume_path(output_dir)
    payload = {
        "source_url": data.get("source_url", ""),
        "resolved_source": data.get("resolved_source", ""),
        "sec_user_id": data.get("sec_user_id", ""),
        "author_nickname": data.get("author_nickname", ""),
        "cursor": data.get("cursor", 0),
        "seen_aweme_ids": list(dict.fromkeys(data.get("seen_aweme_ids", []))),
        "failed_aweme_ids": list(dict.fromkeys(data.get("failed_aweme_ids", []))),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _failure_note(detected_mode, exc):
    raw = note_friendly_error_message(exc)
    text = str(raw or "").strip()
    lowered = text.lower()
    if detected_mode == "video":
        if any(token in lowered for token in ("403", "401", "cookie", "login", "permission denied", "forbidden")):
            return f"视频下载失败：可能需要已登录浏览器 Cookie。{text}"
        if any(token in lowered for token in ("unsupported url", "not found", "404", "no video formats", "extractor")):
            return f"视频下载失败：链接可能失效、被限制或已改版。{text}"
        return f"视频下载失败：{text}"
    if any(token in lowered for token in ("aweme_detail", "empty response", "空响应", "cookie", "login", "403", "401", "forbidden")):
        return f"图文/实况下载失败：可能需要已登录浏览器 Cookie，或者这条作品的详情数据暂时拿不到。{text}"
    if any(token in lowered for token in ("motion", "video", "mp4")):
        return f"图文/实况下载失败：动图资源或预览视频生成失败。{text}"
    return f"图文/实况下载失败：{text}"


def _download_video_as_batch(
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
    return run_share_single(
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
        emit_start=False,
    )


def _download_media_as_batch(
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
    existing_index,
):
    return run_note_single(
        url=url,
        output_dir=output_dir,
        browser_name=browser_name,
        browser_profile=browser_profile,
        retries=retries,
        timeout=timeout,
        overwrite=overwrite,
        existing_index=existing_index,
        progress_callback=progress_callback,
        index=index,
        total=total,
        emit_start=False,
    )


def _without_finish(callback):
    if not callback:
        return None

    def wrapped(event, index, total, payload):
        if event == "finish":
            return
        callback(event, index, total, payload)

    return wrapped


def _is_terminal_skip(result):
    if result.get("status") != "skipped":
        return False
    if result.get("path") or result.get("dir"):
        return True
    note = str(result.get("note") or "").strip().lower()
    if not note:
        return False
    return (
        "file exists" in note
        or "已下载过" in note
        or "重复下载" in note
    )


def _download_with_fallback(
    share_url,
    output_dir,
    browser_name,
    browser_profile,
    retries,
    timeout,
    overwrite,
    progress_callback,
    index,
    total,
    existing_index,
    primary_mode,
):
    mode_order = [primary_mode]
    fallback_mode = "douyin_media" if primary_mode == "video" else "video"
    if fallback_mode not in mode_order:
        mode_order.append(fallback_mode)

    for attempt_index, mode in enumerate(mode_order):
        callback = progress_callback if attempt_index == len(mode_order) - 1 else _without_finish(progress_callback)
        if mode == "douyin_media":
            result = _download_media_as_batch(
                url=share_url,
                output_dir=output_dir,
                browser_name=browser_name,
                browser_profile=browser_profile,
                retries=retries,
                timeout=timeout,
                overwrite=overwrite,
                progress_callback=callback,
                index=index,
                total=total,
                existing_index=existing_index,
            )
        else:
            result = _download_video_as_batch(
                url=share_url,
                output_dir=output_dir,
                browser_name=browser_name,
                browser_profile=browser_profile,
                retries=retries,
                timeout=timeout,
                overwrite=overwrite,
                progress_callback=callback,
                index=index,
                total=total,
            )

        if result.get("status") == "ok":
            if attempt_index < len(mode_order) - 1:
                _emit(progress_callback, "finish", index, total, result)
            return result, mode
        if _is_terminal_skip(result):
            if attempt_index < len(mode_order) - 1:
                _emit(progress_callback, "finish", index, total, result)
            return result, mode

    return result, mode_order[-1]


def run_author_batch(
    source_url,
    output_dir="downloads",
    manifest_path="manifest.csv",
    browser_name="none",
    browser_profile=None,
    retries=3,
    timeout=60,
    overwrite=False,
    progress_callback=None,
    page_count=20,
    max_items=0,
    start_cursor=None,
    _recovery_attempted=False,
    _ignore_resume_seen=False,
    force_browser_fallback=False,
):
    output_dir = str(Path(output_dir))
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    opener = _build_opener(browser_name, browser_profile)
    sec_user_id, resolved_source = _resolve_author_sec_user_id(opener, source_url, timeout)
    existing_index = _load_existing_index(output_dir)
    existing_aweme_index = _load_existing_aweme_index(output_dir)
    resume_state = {} if overwrite else _load_author_resume(output_dir, sec_user_id)
    cursor = resume_state.get("cursor", 0) or 0
    if start_cursor is not None:
        cursor = start_cursor
    if _ignore_resume_seen:
        seen = _rebuild_seen_from_downloads(output_dir)
    else:
        seen = set(str(item) for item in (resume_state.get("seen_aweme_ids") or []) if str(item).strip())
    nickname = str(resume_state.get("author_nickname") or "").strip()
    rows = []
    total = max_items if max_items > 0 else 0
    processed_new = 0

    if resume_state:
        _emit(
            progress_callback,
            "log",
            0,
            total,
            {"message": f"检测到断点记录，已从上次 cursor 继续。"},
        )

    retry_queue = [item for item in (resume_state.get("failed_aweme_ids") or []) if str(item).strip()]
    retry_queue = list(dict.fromkeys(str(item) for item in retry_queue if str(item).strip()))
    if retry_queue:
        _emit(
            progress_callback,
            "log",
            0,
            total,
            {"message": f"检测到 {len(retry_queue)} 条上次失败的作品，先重试。"},
        )
    failed_set = set(retry_queue)

    for aweme_id in list(retry_queue):
        if max_items and processed_new >= max_items:
            break
        share_url = f"https://www.douyin.com/video/{aweme_id}"
        item_index = processed_new + 1
        _emit(progress_callback, "start", item_index, max_items or 0, {"url": share_url})
        _emit(
            progress_callback,
            "log",
            item_index,
            max_items or 0,
            {"message": "正在重试上次失败作品"},
        )
        result, chosen_mode = _download_with_fallback(
            share_url=share_url,
            output_dir=output_dir,
            browser_name=browser_name,
            browser_profile=browser_profile,
            retries=retries,
            timeout=timeout,
            overwrite=overwrite,
            progress_callback=progress_callback,
            index=item_index,
            total=max_items or 0,
            existing_index=existing_index,
            primary_mode="douyin_media",
        )
        result["url"] = share_url
        result["id"] = aweme_id
        if result.get("status") in {"ok", "skipped"}:
            failed_set.discard(aweme_id)
            seen.add(aweme_id)
            processed_new += 1
            if result.get("status") == "ok" and aweme_id:
                marker = result.get("dir") or result.get("path") or ""
                if marker:
                    existing_aweme_index[aweme_id] = Path(marker)
        else:
            result["note"] = result.get("note") or "上次失败的作品本次仍未成功。"
        rows.append(_row_from_result(resolved_source, sec_user_id, nickname, "douyin_media", result))
        _emit(progress_callback, "finish", item_index, max_items or 0, result)
        _save_author_resume(
            output_dir,
            {
                "source_url": source_url,
                "resolved_source": resolved_source,
                "sec_user_id": sec_user_id,
                "author_nickname": nickname,
                "cursor": cursor,
                "seen_aweme_ids": sorted(seen),
                "failed_aweme_ids": sorted(failed_set),
            },
        )

    while True:
        if max_items and processed_new >= max_items:
            break

        page_cursor = cursor
        try:
            payload = _fetch_author_page(opener, sec_user_id, page_cursor, page_count, timeout)
        except (ValueError, urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            _emit(
                progress_callback,
                "log",
                0,
                total,
                {"message": f"作者作品列表接口请求失败：{exc}"},
            )
            break
        aweme_list = payload.get("aweme_list") or []
        if not aweme_list:
            break

        next_cursor = payload.get("max_cursor")
        has_more = bool(payload.get("has_more"))

        for aweme in aweme_list:
            aweme_id = str(aweme.get("aweme_id") or "").strip()
            if not aweme_id:
                continue
            author = aweme.get("author") or {}
            if not nickname:
                nickname = author.get("nickname") or ""
            share_url = _aweme_share_url(aweme)
            detected_mode = _aweme_detected_mode(aweme)
            existing_path = existing_aweme_index.get(aweme_id)
            if not existing_path and detected_mode == "video" and not overwrite:
                matched_video = _locate_existing_video_file(output_dir, aweme)
                if matched_video:
                    existing_aweme_index[aweme_id] = matched_video
                    existing_path = matched_video

            if aweme_id in seen or (existing_path and not overwrite):
                result = {
                    "url": share_url,
                    "id": aweme_id,
                    "status": "skipped",
                    "path": str(existing_path or ""),
                    "dir": str(existing_path) if existing_path and Path(existing_path).is_dir() else "",
                    "note": "该作品已下载过，已跳过重复下载。",
                }
                rows.append(_row_from_result(resolved_source, sec_user_id, nickname, detected_mode, result))
                continue

            item_index = processed_new + 1
            _emit(progress_callback, "start", item_index, max_items or 0, {"url": share_url})
            _emit(
                progress_callback,
                "log",
                item_index,
                max_items or 0,
                {"message": f"已识别作品类型: {'图文 / 动图' if detected_mode == 'douyin_media' else '视频'}"},
            )

            result, chosen_mode = _download_with_fallback(
                share_url=share_url,
                output_dir=output_dir,
                browser_name=browser_name,
                browser_profile=browser_profile,
                retries=retries,
                timeout=timeout,
                overwrite=overwrite,
                progress_callback=progress_callback,
                index=item_index,
                total=max_items or 0,
                existing_index=existing_index,
                primary_mode=detected_mode,
            )
            if result.get("status") == "failed":
                result["note"] = _failure_note(chosen_mode, result.get("note", ""))
                _emit(progress_callback, "finish", item_index, max_items or 0, result)

            if result.get("status") in {"ok", "skipped"}:
                seen.add(aweme_id)
                failed_set.discard(aweme_id)
                processed_new += 1
            if result.get("status") == "ok" and aweme_id:
                marker = result.get("dir") or result.get("path") or ""
                if marker:
                    existing_aweme_index[aweme_id] = Path(marker)
            rows.append(_row_from_result(resolved_source, sec_user_id, nickname, detected_mode, result))
            if result.get("status") == "failed" and aweme_id:
                failed_set.add(aweme_id)
            if aweme_id and result.get("status") in {"ok", "skipped"}:
                _save_author_resume(
                    output_dir,
                    {
                        "source_url": source_url,
                        "resolved_source": resolved_source,
                        "sec_user_id": sec_user_id,
                        "author_nickname": nickname,
                        "cursor": page_cursor,
                        "seen_aweme_ids": sorted(seen),
                        "failed_aweme_ids": sorted(failed_set),
                    },
                )
            elif aweme_id and result.get("status") == "failed":
                _save_author_resume(
                    output_dir,
                    {
                        "source_url": source_url,
                        "resolved_source": resolved_source,
                        "sec_user_id": sec_user_id,
                        "author_nickname": nickname,
                        "cursor": page_cursor,
                        "seen_aweme_ids": sorted(seen),
                        "failed_aweme_ids": sorted(failed_set),
                    },
                )

            if max_items and processed_new >= max_items:
                break

        if max_items and processed_new >= max_items:
            break
        if next_cursor in (None, page_cursor):
            break
        cursor = next_cursor
        if seen:
            _save_author_resume(
                output_dir,
                {
                    "source_url": source_url,
                    "resolved_source": resolved_source,
                    "sec_user_id": sec_user_id,
                    "author_nickname": nickname,
                    "cursor": cursor,
                    "seen_aweme_ids": sorted(seen),
                    "failed_aweme_ids": sorted(failed_set),
                },
            )
        if not has_more:
            break

    all_skipped = all(row.get("status") == "skipped" for row in rows) if rows else False
    should_trigger_browser = force_browser_fallback or not rows or all_skipped
    if should_trigger_browser:
        if (resume_state or cursor) and not _recovery_attempted and not overwrite:
            _emit(
                progress_callback,
                "log",
                0,
                total,
                {"message": "当前断点已经跑到末尾，正在从头补扫一次漏掉的作品。"},
            )
            return run_author_batch(
                source_url=source_url,
                output_dir=output_dir,
                manifest_path=manifest_path,
                browser_name=browser_name,
                browser_profile=browser_profile,
                retries=retries,
                timeout=timeout,
                overwrite=overwrite,
                progress_callback=progress_callback,
                page_count=page_count,
                max_items=max_items,
                start_cursor=0,
                _recovery_attempted=True,
                _ignore_resume_seen=True,
                force_browser_fallback=force_browser_fallback,
            )
        if not overwrite:
            recovered_seen = _rebuild_seen_from_downloads(output_dir)
            try:
                author_page_url = f"https://www.douyin.com/user/{sec_user_id}"
                browser_aweme_items = _collect_browser_aweme_ids(
                    author_page_url,
                    timeout=timeout,
                    max_items=max_items or 0,
                    progress_callback=progress_callback,
                )
            except Exception as exc:
                _emit(
                    progress_callback,
                    "log",
                    0,
                    total,
                    {"message": f"浏览器回补未启用：{exc}"},
                )
            else:
                missing_aweme_items = [item for item in browser_aweme_items if item["id"] not in recovered_seen]
                if missing_aweme_items:
                    _emit(
                        progress_callback,
                        "log",
                        0,
                        total,
                        {"message": f"浏览器回补发现 {len(missing_aweme_items)} 条疑似漏掉的作品，开始补下。"},
                    )
                    browser_rows = []
                    for item in missing_aweme_items:
                        aweme_id = item["id"]
                        link_type = item["type"]
                        if max_items and processed_new >= max_items:
                            break
                        if link_type == "note":
                            share_url = f"https://www.douyin.com/note/{aweme_id}"
                            primary_mode = "douyin_media"
                        else:
                            share_url = f"https://www.douyin.com/video/{aweme_id}"
                            primary_mode = "video"
                        item_index = processed_new + 1
                        _emit(progress_callback, "start", item_index, max_items or 0, {"url": share_url})
                        result, chosen_mode = _download_with_fallback(
                            share_url=share_url,
                            output_dir=output_dir,
                            browser_name=browser_name,
                            browser_profile=browser_profile,
                            retries=retries,
                            timeout=timeout,
                            overwrite=overwrite,
                            progress_callback=progress_callback,
                            index=item_index,
                            total=max_items or 0,
                            existing_index=existing_index,
                            primary_mode=primary_mode,
                        )
                        result["url"] = share_url
                        result["id"] = aweme_id
                        if result.get("status") == "failed":
                            result["note"] = _failure_note(chosen_mode, result.get("note", ""))
                            _emit(progress_callback, "finish", item_index, max_items or 0, result)
                            failed_set.add(aweme_id)
                        else:
                            seen.add(aweme_id)
                            failed_set.discard(aweme_id)
                            processed_new += 1
                            marker = result.get("dir") or result.get("path") or ""
                            if marker:
                                existing_aweme_index[aweme_id] = Path(marker)
                        browser_rows.append(_row_from_result(resolved_source, sec_user_id, nickname, chosen_mode, result))
                    if browser_rows:
                        rows.extend(browser_rows)
                        write_manifest(manifest_path, rows)
                        summary = summarize_rows(rows)
                        _save_author_resume(
                            output_dir,
                            {
                                "source_url": source_url,
                                "resolved_source": resolved_source,
                                "sec_user_id": sec_user_id,
                                "author_nickname": nickname,
                                "cursor": cursor,
                                "seen_aweme_ids": sorted(seen),
                                "failed_aweme_ids": sorted(failed_set),
                            },
                        )
                        _emit(progress_callback, "summary", processed_new, max_items or processed_new, summary)
                        return rows, summary
        _emit(
            progress_callback,
            "log",
            0,
            total,
            {"message": "没有更多作品可继续抓取。"},
        )
        summary = summarize_rows(rows)
        _emit(progress_callback, "summary", processed_new, max_items or processed_new, summary)
        return rows, summary

    write_manifest(manifest_path, rows)
    summary = summarize_rows(rows)
    _emit(progress_callback, "summary", processed_new, max_items or processed_new, summary)
    return rows, summary
