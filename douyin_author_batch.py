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


DETAIL_URL = "https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id={aweme_id}"
DETAIL_URL_WEBAPP = (
    "https://www.douyin.com/aweme/v1/web/aweme/detail/"
    "?aweme_id={aweme_id}"
    "&aid=6383"
    "&device_platform=webapp"
)


def _fetch_aweme_detail(opener, aweme_id, timeout):
    """Fetch a single aweme's detail for create_time and type detection."""
    headers = {
        "Referer": f"https://www.douyin.com/video/{aweme_id}",
        "Accept": "application/json, text/plain, */*",
    }
    for url in [DETAIL_URL.format(aweme_id=aweme_id),
                DETAIL_URL_WEBAPP.format(aweme_id=aweme_id)]:
        try:
            with _open(opener, url, timeout, headers=headers) as response:
                payload = json.loads(response.read().decode("utf-8"))
            detail = payload.get("aweme_detail")
            if isinstance(detail, dict):
                return detail
        except Exception:
            continue
    return None


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

    # Scan .meta/ subdirectory for video metadata (new location)
    meta_dir = root / ".meta"
    scan_dirs = [root]
    if meta_dir.is_dir():
        scan_dirs.append(meta_dir)
    for scan_dir in scan_dirs:
        for child in scan_dir.iterdir():
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
    # Normalize old format (plain string IDs) to new format (dicts with mode)
    normalized = []
    for item in data["failed_aweme_ids"]:
        if isinstance(item, dict) and item.get("id"):
            normalized.append(item)
        elif isinstance(item, str) and item.strip():
            normalized.append({"id": item.strip(), "mode": "video"})
    data["failed_aweme_ids"] = normalized
    return data


def _save_author_resume(output_dir, data):
    path = _author_resume_path(output_dir)
    # Deduplicate failed_aweme_ids by "id", preserving last occurrence (and its mode)
    seen_ids = {}
    for item in data.get("failed_aweme_ids", []):
        if isinstance(item, dict) and item.get("id"):
            seen_ids[item["id"]] = item
        elif isinstance(item, str) and item.strip():
            seen_ids[item.strip()] = {"id": item.strip(), "mode": "video"}
    failed_aweme_ids = list(seen_ids.values())
    payload = {
        "source_url": data.get("source_url", ""),
        "resolved_source": data.get("resolved_source", ""),
        "sec_user_id": data.get("sec_user_id", ""),
        "author_nickname": data.get("author_nickname", ""),
        "cursor": data.get("cursor", 0),
        "seen_aweme_ids": list(dict.fromkeys(data.get("seen_aweme_ids", []))),
        "failed_aweme_ids": failed_aweme_ids,
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


def _browser_video_links(page):
    """Extract aweme IDs from the author's own video/note links, excluding recommendations."""
    return set(page.evaluate("""
        () => {
          const ids = new Set();
          // Find the main content area (author's post list)
          const main = document.querySelector('[class*="post"]')
            || document.querySelector('main')
            || document.querySelector('[role="main"]')
            || document.documentElement;
          // Exclude recommendation, sidebar, related sections
          const exclude = 'nav, header, footer, aside, [class*="recommend"], [class*="sidebar"], [class*="related"], [class*="suggest"]';
          const excludeEls = main.querySelectorAll(exclude);
          const isExcluded = (el) => {
            for (const ex of excludeEls) { if (ex.contains(el)) return true; }
            return false;
          };
          for (const a of main.querySelectorAll('a[href]')) {
            if (isExcluded(a)) continue;
            const href = a.getAttribute('href') || '';
            const m = href.match(/\\/(video|note|slides)\\/(\\d{15,20})/);
            if (m) ids.add(m[2]);
          }
          return [...ids];
        }
    """))


def _collect_browser_aweme_ids(sec_user_id, browser_name, browser_profile, progress_callback):
    """Use Playwright to scroll through the author page and collect all aweme IDs."""
    from playwright.sync_api import sync_playwright

    url = f"https://www.douyin.com/user/{sec_user_id}"

    _emit(progress_callback, "log", 0, 0,
          {"message": "浏览器回补：打开作者主页扫描遗漏作品..."})

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = browser.new_context(
            viewport={"width": 1400, "height": 1080},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        )
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        """)
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(4000)

            # Always wait for user to confirm login in the terminal
            _emit(progress_callback, "log", 0, 0,
                  {"message": "请在弹出的浏览器中完成登录，然后回到终端按回车继续。"})
            try:
                input("[浏览器回补] 登录完成后按回车继续...")
            except (EOFError, OSError):
                _emit(progress_callback, "log", 0, 0,
                      {"message": "无终端输入，等待 3 分钟供手动登录..."})
                page.wait_for_timeout(180000)
                _emit(progress_callback, "log", 0, 0,
                      {"message": "等待结束，继续扫描。"})

            page.wait_for_timeout(3000)

            # Scroll through the page to load all videos
            _emit(progress_callback, "log", 0, 0, {"message": "开始滚动页面收集视频..."})

            # Find the actual scrollable container (Douyin uses an internal div)
            scroll_container = page.evaluate("""
                () => {
                  // Try common content containers
                  const candidates = document.querySelectorAll(
                    'main, [role="main"], [class*="scroll"], [class*="list"], [class*="feed"], [class*="post"]'
                  );
                  for (const el of candidates) {
                    const s = getComputedStyle(el);
                    if ((s.overflowY === 'auto' || s.overflowY === 'scroll') && el.scrollHeight > el.clientHeight) {
                      return true;  // found it, will scroll via this method
                    }
                  }
                  // Broader search
                  for (const el of document.querySelectorAll('div, section')) {
                    const s = getComputedStyle(el);
                    if ((s.overflowY === 'auto' || s.overflowY === 'scroll') && el.scrollHeight > el.clientHeight + 100) {
                      return true;
                    }
                  }
                  return false;
                }
            """)

            scroll_script = """
                () => {
                  const candidates = document.querySelectorAll(
                    'main, [role="main"], [class*="scroll"], [class*="list"], [class*="feed"], [class*="post"]'
                  );
                  for (const el of candidates) {
                    const s = getComputedStyle(el);
                    if ((s.overflowY === 'auto' || s.overflowY === 'scroll') && el.scrollHeight > el.clientHeight) {
                      el.scrollBy(0, el.clientHeight * 3);
                      return el.scrollTop + el.clientHeight >= el.scrollHeight - 50;
                    }
                  }
                  for (const el of document.querySelectorAll('div, section')) {
                    const s = getComputedStyle(el);
                    if ((s.overflowY === 'auto' || s.overflowY === 'scroll') && el.scrollHeight > el.clientHeight + 100) {
                      el.scrollBy(0, el.clientHeight * 3);
                      return el.scrollTop + el.clientHeight >= el.scrollHeight - 50;
                    }
                  }
                  window.scrollTo(0, document.body.scrollHeight);
                  return false;
                }
            """

            _emit(progress_callback, "log", 0, 0,
                  {"message": f"找到内部滚动容器: {scroll_container}"})

            last_count = 0
            no_new_count = 0
            scroll_count = 0
            max_no_new = 8

            while no_new_count < max_no_new:
                page.evaluate(scroll_script)
                page.wait_for_timeout(2500)

                scroll_count += 1

                no_more = page.query_selector('text="暂时没有更多了"')
                no_more2 = page.query_selector('text="已经到底了"')

                ids = _browser_video_links(page)

                if len(ids) == last_count:
                    no_new_count += 1
                else:
                    no_new_count = 0
                last_count = len(ids)

                _emit(progress_callback, "log", 0, 0,
                      {"message": f"  第 {scroll_count} 次滚动，当前找到 {len(ids)} 条作品。"})

                if no_more or no_more2:
                    _emit(progress_callback, "log", 0, 0,
                          {"message": "检测到页面提示已到底部。"})
                    break

            ids = _browser_video_links(page)
            _emit(progress_callback, "log", 0, 0,
                  {"message": f"页面扫描完成，共发现 {len(ids)} 条作品。"})
            return ids
        finally:
            browser.close()


def _parse_date_start(value):
    """Parse 'YYYY-MM-DD' into a Unix timestamp (start of that day)."""
    value = str(value or "").strip()
    if not value:
        return None
    try:
        dt = datetime.datetime.strptime(value, "%Y-%m-%d")
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return None


def _parse_date_end(value):
    """Parse 'YYYY-MM-DD' into a Unix timestamp (end of that day, 23:59:59)."""
    value = str(value or "").strip()
    if not value:
        return None
    try:
        dt = datetime.datetime.strptime(value, "%Y-%m-%d")
        return int(dt.timestamp()) + 86399
    except (ValueError, TypeError):
        return None


def _format_date_range(start_ts, end_ts):
    parts = []
    if start_ts:
        parts.append(datetime.datetime.fromtimestamp(start_ts).strftime("%Y-%m-%d"))
    if end_ts:
        parts.append(datetime.datetime.fromtimestamp(end_ts).strftime("%Y-%m-%d"))
    return " 至 ".join(parts) if parts else ""


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
    date_start="",
    date_end="",
):
    output_dir = str(Path(output_dir))
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    opener = _build_opener(browser_name, browser_profile)
    sec_user_id, resolved_source = _resolve_author_sec_user_id(opener, source_url, timeout)
    existing_index = _load_existing_index(output_dir)
    existing_aweme_index = _load_existing_aweme_index(output_dir)
    nickname = ""
    rows = []
    processed_new = 0
    cursor = 0
    api_total_items = 0
    seen_aweme_ids: set[str] = set()

    start_ts = _parse_date_start(date_start)  # inclusive, None if not set
    end_ts = _parse_date_end(date_end)        # inclusive end-of-day, None if not set

    range_desc = _format_date_range(start_ts, end_ts)
    if max_items > 0:
        _emit(progress_callback, "log", 0, 0, {"message": f"最多下载 {max_items} 条新作品。"})
    elif range_desc:
        _emit(progress_callback, "log", 0, 0, {"message": f"下载日期范围: {range_desc}"})
    else:
        _emit(progress_callback, "log", 0, 0, {"message": "下载全部作品。"})

    while True:
        try:
            payload = _fetch_author_page(opener, sec_user_id, cursor, page_count, timeout)
        except (ValueError, urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            _emit(progress_callback, "log", 0, 0, {"message": f"作者作品列表接口请求失败：{exc}"})
            break
        aweme_list = payload.get("aweme_list") or []
        if not aweme_list:
            break

        api_total_items += len(aweme_list)
        next_cursor = payload.get("max_cursor")
        has_more = bool(payload.get("has_more"))
        past_date_range = False
        _emit(progress_callback, "log", 0, 0,
              {"message": f"API 翻页: 本页 {len(aweme_list)} 条，累计 {api_total_items} 条，has_more={has_more}"})

        for aweme in aweme_list:
            aweme_id = str(aweme.get("aweme_id") or "").strip()
            if not aweme_id:
                continue
            seen_aweme_ids.add(aweme_id)
            author = aweme.get("author") or {}
            if not nickname:
                nickname = author.get("nickname") or ""
            create_time = aweme.get("create_time")
            share_url = _aweme_share_url(aweme)
            detected_mode = _aweme_detected_mode(aweme)

            # Date range filter (API returns newest first)
            if start_ts and create_time:
                try:
                    if int(create_time) < start_ts:
                        past_date_range = True
                        break
                except (TypeError, ValueError):
                    pass
            if end_ts and create_time:
                try:
                    if int(create_time) > end_ts:
                        continue
                except (TypeError, ValueError):
                    pass

            # Skip already downloaded
            existing_path = existing_aweme_index.get(aweme_id)
            if not existing_path and detected_mode == "video" and not overwrite:
                matched_video = _locate_existing_video_file(output_dir, aweme)
                if matched_video:
                    existing_aweme_index[aweme_id] = matched_video
                    existing_path = matched_video
            if existing_path and not overwrite:
                rows.append({
                    "source": resolved_source, "author_sec_user_id": sec_user_id,
                    "author_nickname": nickname, "url": share_url, "aweme_id": aweme_id,
                    "detected_mode": detected_mode, "status": "skipped",
                    "path": str(existing_path), "dir": "", "publish_time": "",
                    "title": "", "kind": "", "image_count": 0, "motion_count": 0,
                    "audio_path": "", "video_path": "", "ext": "", "extractor": "",
                    "note": "已下载过，已跳过。",
                })
                continue

            # Download
            item_index = processed_new + 1
            _emit(progress_callback, "start", item_index, 0, {"url": share_url})
            _emit(progress_callback, "log", item_index, 0,
                  {"message": f"作品类型: {'图文 / 动图' if detected_mode == 'douyin_media' else '视频'}"})

            result, chosen_mode = _download_with_fallback(
                share_url=share_url, output_dir=output_dir,
                browser_name=browser_name, browser_profile=browser_profile,
                retries=retries, timeout=timeout, overwrite=overwrite,
                progress_callback=progress_callback, index=item_index, total=0,
                existing_index=existing_index, primary_mode=detected_mode,
            )
            if result.get("status") == "failed":
                result["note"] = _failure_note(chosen_mode, result.get("note", ""))
                _emit(progress_callback, "finish", item_index, 0, result)
            if result.get("status") in {"ok", "skipped"}:
                processed_new += 1
            if result.get("status") == "ok" and aweme_id:
                marker = result.get("dir") or result.get("path") or ""
                if marker:
                    existing_aweme_index[aweme_id] = Path(marker)
            rows.append(_row_from_result(resolved_source, sec_user_id, nickname, chosen_mode, result))
            if max_items and processed_new >= max_items:
                break

        if max_items and processed_new >= max_items:
            break
        if past_date_range:
            _emit(progress_callback, "log", 0, 0, {"message": "已扫描到日期范围之外，停止翻页。"})
            break
        if next_cursor in (None, cursor):
            break
        cursor = next_cursor
        if not has_more:
            break

    _emit(progress_callback, "log", 0, 0,
          {"message": f"API 阶段完成：累计看到 {len(seen_aweme_ids)} 条作品，"
                      f"新下载 {processed_new} 条。"})

    # Browser fallback: discover any aweme_ids the API missed.
    # Respect download range: filter by date or count same as API phase.
    if seen_aweme_ids:
        try:
            existing_aweme_ids = set(existing_aweme_index.keys())
            browser_ids = _collect_browser_aweme_ids(
                sec_user_id, browser_name, browser_profile, progress_callback)
            if browser_ids:
                missing = browser_ids - seen_aweme_ids - existing_aweme_ids
                _emit(progress_callback, "log", 0, 0,
                      {"message": f"浏览器回补：页面发现 {len(browser_ids)} 条，"
                                  f"API 未返回 {len(missing)} 条。"})
                if missing:
                    # Fetch detail for each missing item to get create_time and type
                    missing_details = []
                    for aid in sorted(missing):
                        detail = _fetch_aweme_detail(opener, aid, timeout)
                        if not detail:
                            continue
                        ct = detail.get("create_time")
                        missing_details.append((aid, ct, detail))

                    # Date range filter (same as API phase)
                    if start_ts or end_ts:
                        before = len(missing_details)
                        filtered = []
                        for aid, ct, detail in missing_details:
                            if start_ts and ct:
                                try:
                                    if int(ct) < start_ts:
                                        continue
                                except (TypeError, ValueError):
                                    continue
                            if end_ts and ct:
                                try:
                                    if int(ct) > end_ts:
                                        continue
                                except (TypeError, ValueError):
                                    continue
                            filtered.append((aid, ct, detail))
                        missing_details = filtered
                        _emit(progress_callback, "log", 0, 0,
                              {"message": f"日期过滤：{before} 条 -> {len(missing_details)} 条在范围内。"})

                    # Count limit
                    if max_items:
                        remaining = max_items - processed_new
                        if remaining <= 0:
                            missing_details = []
                        else:
                            missing_details = missing_details[:remaining]
                            _emit(progress_callback, "log", 0, 0,
                                  {"message": f"数量限制：仅回补前 {len(missing_details)} 条。"})

                    for aid, ct, detail in missing_details:
                        detected_mode = _aweme_detected_mode(detail)
                        share_url = f"https://www.douyin.com/note/{aid}" if detected_mode == "douyin_media" else f"https://www.douyin.com/video/{aid}"
                        _emit(progress_callback, "log", 0, 0,
                              {"message": f"回补下载: {aid} ({'图文' if detected_mode == 'douyin_media' else '视频'})"})
                        result, chosen_mode = _download_with_fallback(
                            share_url=share_url, output_dir=output_dir,
                            browser_name=browser_name, browser_profile=browser_profile,
                            retries=retries, timeout=timeout, overwrite=overwrite,
                            progress_callback=progress_callback,
                            index=processed_new + 1, total=0,
                            existing_index=existing_index,
                            primary_mode=detected_mode,
                        )
                        if result.get("status") == "failed":
                            result["note"] = _failure_note(chosen_mode, result.get("note", ""))
                            _emit(progress_callback, "finish", processed_new + 1, 0, result)
                        if result.get("status") in {"ok", "skipped"}:
                            processed_new += 1
                        if result.get("status") == "ok":
                            marker = result.get("dir") or result.get("path") or ""
                            if marker:
                                existing_aweme_index[aid] = Path(marker)
                        rows.append(_row_from_result(source_url, sec_user_id, nickname,
                                                     chosen_mode, result))
                        if max_items and processed_new >= max_items:
                            break
                    _emit(progress_callback, "log", 0, 0,
                          {"message": f"浏览器回补完成，补充处理 {len(missing_details)} 条。"})
        except Exception as exc:
            _emit(progress_callback, "log", 0, 0,
                  {"message": f"浏览器回补失败（不影响已下载内容）：{exc}"})

    write_manifest(manifest_path, rows)
    summary = summarize_rows(rows)
    _emit(progress_callback, "summary", processed_new, processed_new, summary)
    return rows, summary
