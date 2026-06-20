#!/usr/bin/env python3
import argparse
import csv
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
)
CHUNK_SIZE = 1024 * 512
URL_PATTERN = re.compile(r"https?://[^\s]+", flags=re.IGNORECASE)
SHARE_DOMAINS = {
    "v.douyin.com",
    "www.douyin.com",
    "douyin.com",
    "iesdouyin.com",
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Batch download authorized video files from direct URLs."
    )
    parser.add_argument(
        "input",
        nargs="?",
        default="urls.txt",
        help="Text file with one URL per line. Default: urls.txt",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        default="downloads",
        help="Directory to save downloaded files. Default: downloads",
    )
    parser.add_argument(
        "--manifest",
        default="manifest.csv",
        help="CSV file used to record download results. Default: manifest.csv",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Number of retries per URL. Default: 3",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Request timeout in seconds. Default: 60",
    )
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        help='Extra HTTP header in the form "Name: Value". Can be used multiple times.',
    )
    parser.add_argument(
        "--referer",
        help="Optional Referer header for hosts that require it.",
    )
    parser.add_argument(
        "--cookie",
        help="Optional raw Cookie header.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing files instead of skipping them.",
    )
    return parser.parse_args()


def load_urls(path):
    return parse_urls_text(Path(path).read_text(encoding="utf-8"))


def parse_urls_text(text):
    urls = []
    for line in text.splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        urls.append(extract_url(value))
    return urls


def extract_url(text):
    match = URL_PATTERN.search(text.strip())
    if not match:
        return text.strip()
    return match.group(0).rstrip("),.;!?\"'")


def normalize_headers(items, referer=None, cookie=None):
    headers = {"User-Agent": DEFAULT_UA}
    for item in items:
        if ":" not in item:
            raise ValueError(f"Invalid header: {item!r}")
        key, value = item.split(":", 1)
        headers[key.strip()] = value.strip()
    if referer:
        headers["Referer"] = referer
    if cookie:
        headers["Cookie"] = cookie
    return headers


def parse_headers_text(text):
    return [line.strip() for line in text.splitlines() if line.strip()]


def validate_source_url(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"不是有效的 http/https 地址：{url}")

    host = (parsed.netloc or "").lower()
    if host in SHARE_DOMAINS or host.endswith(".douyin.com"):
        raise ValueError(
            "检测到这是抖音分享页链接，不是可直接下载的视频文件地址。"
            "当前工具只支持你已获授权的直链文件地址。"
        )


def safe_name(text):
    cleaned = re.sub(r"[^\w.\-]+", "_", text, flags=re.UNICODE).strip("._")
    return cleaned or "file"


def infer_name(url, headers):
    cd = headers.get("Content-Disposition", "")
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd, flags=re.I)
    if match:
        return safe_name(urllib.parse.unquote(match.group(1)))
    path_name = Path(urllib.parse.urlparse(url).path).name
    if path_name:
        return safe_name(urllib.parse.unquote(path_name))
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    return f"{digest}.mp4"


def content_length(headers):
    value = headers.get("Content-Length")
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def open_request(url, headers, timeout):
    request = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(request, timeout=timeout)


def ensure_parent(path):
    path.parent.mkdir(parents=True, exist_ok=True)


def download_one(url, output_dir, headers, timeout, retries, overwrite):
    last_error = None
    validate_source_url(url)
    for attempt in range(1, retries + 1):
        tmp_path = None
        try:
            with open_request(url, headers, timeout) as response:
                meta = dict(response.info().items())
                media_type = (meta.get("Content-Type", "") or "").split(";", 1)[0].strip()
                if media_type.startswith("text/") or media_type in {
                    "application/json",
                    "application/javascript",
                }:
                    raise ValueError(
                        f"返回内容是 {media_type or 'unknown'}，看起来是网页或接口响应，不是视频文件。"
                    )
                file_name = infer_name(url, meta)
                target = Path(output_dir) / file_name
                ensure_parent(target)
                if target.exists() and not overwrite:
                    return {
                        "url": url,
                        "status": "skipped",
                        "path": str(target),
                        "bytes": target.stat().st_size,
                        "content_type": meta.get("Content-Type", ""),
                        "note": "file exists",
                    }

                tmp_path = target.with_suffix(target.suffix + ".part")
                total = content_length(meta)
                downloaded = 0
                sha256 = hashlib.sha256()

                with tmp_path.open("wb") as fh:
                    while True:
                        chunk = response.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        fh.write(chunk)
                        sha256.update(chunk)
                        downloaded += len(chunk)

                if total is not None and downloaded != total:
                    raise IOError(
                        f"incomplete download: expected {total} bytes, got {downloaded}"
                    )

                tmp_path.replace(target)
                return {
                    "url": url,
                    "status": "ok",
                    "path": str(target),
                    "bytes": downloaded,
                    "content_type": meta.get("Content-Type", ""),
                    "sha256": sha256.hexdigest(),
                    "note": "",
                }
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, IOError, ValueError) as exc:
            last_error = exc
            if tmp_path and tmp_path.exists():
                tmp_path.unlink()
            if attempt < retries:
                time.sleep(min(2 * attempt, 5))

    return {
        "url": url,
        "status": "failed",
        "path": "",
        "bytes": 0,
        "content_type": "",
        "note": str(last_error) if last_error else "unknown error",
    }


def write_manifest(path, rows):
    fieldnames = ["url", "status", "path", "bytes", "content_type", "sha256", "note"]
    with Path(path).open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})


def print_summary(rows):
    print(json.dumps(summarize_rows(rows), ensure_ascii=False, indent=2))


def summarize_rows(rows):
    counts = {"ok": 0, "skipped": 0, "failed": 0}
    total_bytes = 0
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        if row["status"] == "ok":
            total_bytes += int(row.get("bytes") or 0)
    return {
        "ok": counts.get("ok", 0),
        "skipped": counts.get("skipped", 0),
        "failed": counts.get("failed", 0),
        "downloaded_bytes": total_bytes,
    }


def run_batch(
    urls,
    output_dir="downloads",
    manifest_path="manifest.csv",
    header_items=None,
    referer=None,
    cookie=None,
    retries=3,
    timeout=60,
    overwrite=False,
    progress_callback=None,
):
    if not urls:
        raise ValueError("No URLs found.")

    headers = normalize_headers(header_items or [], referer, cookie)
    rows = []
    total = len(urls)

    for idx, url in enumerate(urls, start=1):
        if progress_callback:
            progress_callback("start", idx, total, {"url": url})
        row = download_one(
            url=url,
            output_dir=output_dir,
            headers=headers,
            timeout=timeout,
            retries=retries,
            overwrite=overwrite,
        )
        rows.append(row)
        if progress_callback:
            progress_callback("finish", idx, total, row)

    write_manifest(manifest_path, rows)
    summary = summarize_rows(rows)
    if progress_callback:
        progress_callback("summary", total, total, summary)
    return rows, summary


def main():
    args = parse_args()
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Input file not found: {input_path}", file=sys.stderr)
        return 1

    try:
        urls = load_urls(input_path)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if not urls:
        print("No URLs found in input file.", file=sys.stderr)
        return 3

    try:
        rows, _summary = run_batch(
            urls=urls,
            output_dir=args.output_dir,
            manifest_path=args.manifest,
            header_items=args.header,
            referer=args.referer,
            cookie=args.cookie,
            retries=args.retries,
            timeout=args.timeout,
            overwrite=args.overwrite,
            progress_callback=_cli_progress,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    return 0 if not any(row["status"] == "failed" for row in rows) else 4


def _cli_progress(event, index, total, payload):
    if event == "start":
        print(f"[{index}/{total}] {payload['url']}")
    elif event == "finish":
        print(f"  -> {payload['status']}: {payload.get('path') or payload.get('note', '')}")
    elif event == "summary":
        print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
