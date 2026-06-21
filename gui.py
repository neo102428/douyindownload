#!/usr/bin/env python3
import argparse
import json
import mimetypes
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socket import socket

from douyin_author_batch import run_author_batch, runtime_info as author_runtime_info
from douyin_gallerydl import run_image_batch, runtime_info as image_runtime_info
from douyin_note import run_note_batch, runtime_info as note_runtime_info
from douyin_ytdlp import parse_share_text, run_share_batch, runtime_info as video_runtime_info


WORKSPACE = Path(__file__).resolve().parent
STATIC_DIR = WORKSPACE / "web"
DEFAULT_URLS_PATH = WORKSPACE / "urls.txt"
DEFAULT_OUTPUT_DIR = WORKSPACE / "downloads"
DEFAULT_MANIFEST_PATH = WORKSPACE / "manifest.csv"


def runtime_info():
    return {
        "video": video_runtime_info(),
        "tiktok_image": image_runtime_info(),
        "douyin_media": note_runtime_info(),
        "douyin_author_auto": author_runtime_info(),
    }


class DownloadCancelled(Exception):
    """Raised when the user cancels a download."""


class DownloadState:
    def __init__(self):
        self._lock = threading.Lock()
        self._worker = None
        self._pause_event = threading.Event()
        self._cancel_flag = False
        self._reset()

    def _reset(self):
        self.running = False
        self.paused = False
        self.status = "就绪"
        self.current = 0
        self.total = 0
        self.current_url = ""
        self.logs = []
        self.summary = None
        self.last_error = ""
        self.output_dir = str(DEFAULT_OUTPUT_DIR)
        self.manifest_path = str(DEFAULT_MANIFEST_PATH)
        self.author_url = ""
        self.download_range = "all"
        self.date_start = ""
        self.date_end = ""
        self._pause_event.set()
        self._cancel_flag = False

    def defaults(self):
        urls_text = ""
        if DEFAULT_URLS_PATH.exists():
            urls_text = DEFAULT_URLS_PATH.read_text(encoding="utf-8")
        return {
            "urls_text": urls_text,
            "output_dir": str(DEFAULT_OUTPUT_DIR),
            "manifest_path": str(DEFAULT_MANIFEST_PATH),
            "download_mode": "video",
            "retries": 3,
            "timeout": 60,
            "overwrite": False,
            "author_url": "",
            "download_range": "all",
            "date_start": "",
            "date_end": "",
            "browser_name": "none",
            "browser_profile": "",
            "runtime": runtime_info(),
        }

    def snapshot(self):
        with self._lock:
            return {
                "running": self.running,
                "paused": self.paused,
                "status": self.status,
                "current": self.current,
                "total": self.total,
                "current_url": self.current_url,
                "logs": list(self.logs),
                "summary": self.summary,
                "last_error": self.last_error,
                "output_dir": self.output_dir,
                "manifest_path": self.manifest_path,
                "download_mode": getattr(self, "download_mode", "video"),
                "author_url": self.author_url,
                "download_range": self.download_range,
                "date_start": self.date_start,
                "date_end": self.date_end,
                "runtime": runtime_info(),
            }

    def pause(self):
        with self._lock:
            if not self.running:
                raise RuntimeError("没有正在运行的下载任务。")
            if self.paused:
                raise RuntimeError("任务已经暂停了。")
            self._pause_event.clear()
            self.paused = True
            self.status = "已暂停"
            self.logs.append("用户暂停了下载。")
        return self.snapshot()

    def resume(self):
        with self._lock:
            if not self.running:
                raise RuntimeError("没有正在运行的下载任务。")
            if not self.paused:
                raise RuntimeError("任务没有在暂停状态。")
            self._pause_event.set()
            self.paused = False
            self.status = "下载中"
            self.logs.append("用户恢复了下载。")
        return self.snapshot()

    def cancel(self):
        with self._lock:
            if not self.running:
                raise RuntimeError("没有正在运行的下载任务。")
            self._cancel_flag = True
            self._pause_event.set()
            self.paused = False
            self.status = "正在取消"
            self.logs.append("用户取消了下载，当前文件完成后停止。")
        return self.snapshot()

    def save_urls_text(self, urls_text):
        DEFAULT_URLS_PATH.write_text(urls_text, encoding="utf-8")

    def start(self, payload):
        download_mode = str(payload.get("download_mode", "video")).strip() or "video"
        urls_text = str(payload.get("urls_text", ""))
        urls = parse_share_text(urls_text)
        author_url = str(payload.get("author_url", "")).strip()
        download_range = str(payload.get("download_range", "all")).strip() or "all"
        date_start = str(payload.get("date_start", "")).strip()
        date_end = str(payload.get("date_end", "")).strip()

        if download_range == "last50":
            author_max_items = 50
        elif download_range == "last100":
            author_max_items = 100
        elif download_range == "date_range":
            author_max_items = 0
        else:
            author_max_items = 0

        if download_mode == "douyin_author_auto":
            if not author_url:
                raise ValueError("请先输入抖音作者主页链接或作者分享链接。")
            if download_range == "date_range" and not date_start and not date_end:
                raise ValueError("请至少填写一个日期（开始日期或结束日期）。")
        elif not urls:
            raise ValueError("请先在左侧输入框里粘贴抖音分享文案或分享链接，一行一个。")

        output_dir = str(payload.get("output_dir", "")).strip() or str(DEFAULT_OUTPUT_DIR)
        manifest_path = str(payload.get("manifest_path", "")).strip() or str(
            DEFAULT_MANIFEST_PATH
        )
        retries = max(1, int(payload.get("retries", 3)))
        timeout = max(5, int(payload.get("timeout", 60)))
        overwrite = bool(payload.get("overwrite", False))
        browser_name = str(payload.get("browser_name", "none")).strip() or "none"
        browser_profile = str(payload.get("browser_profile", "")).strip() or None

        if download_mode not in {"video", "douyin_media", "image", "douyin_author_auto"}:
            raise ValueError("下载模式无效。")

        with self._lock:
            if self.running:
                raise RuntimeError("已有下载任务正在运行，请等当前任务完成。")
            self._reset()
            self.running = True
            self.status = "准备开始"
            self.total = len(urls)
            self.output_dir = output_dir
            self.manifest_path = manifest_path
            self.download_mode = download_mode
            self.author_url = author_url
            self.download_range = download_range
            self.date_start = date_start
            self.date_end = date_end
            mode_label = {
                "video": "视频模式",
                "douyin_media": "抖音图文 / 动图模式",
                "image": "TikTok 图文模式",
                "douyin_author_auto": "抖音作者主页批量模式",
            }.get(download_mode, "下载模式")
            self.logs.append(f"已创建任务，当前为{mode_label}。")
            if download_mode == "douyin_author_auto":
                self.logs.append(f"作者主页: {author_url}")
                range_labels = {
                    "all": "全部下载",
                    "last50": "最近 50 条",
                    "last100": "最近 100 条",
                    "date_range": f"日期范围: {date_start or '…'} 至 {date_end or '…'}",
                }
                self.logs.append(f"下载范围: {range_labels.get(download_range, '全部下载')}")
            else:
                self.logs.append(f"共 {len(urls)} 条链接。")

        self.save_urls_text(urls_text)

        options = {
            "output_dir": output_dir,
            "manifest_path": manifest_path,
            "download_mode": download_mode,
            "browser_name": browser_name,
            "browser_profile": browser_profile,
            "retries": retries,
            "timeout": timeout,
            "overwrite": overwrite,
        }
        if download_mode != "douyin_author_auto":
            options["urls"] = urls
        else:
            options["author_url"] = author_url
            options["max_items"] = author_max_items
            options["date_start"] = date_start
            options["date_end"] = date_end
        self._worker = threading.Thread(target=self._run, args=(options,), daemon=True)
        self._worker.start()
        return self.snapshot()

    def _run(self, options):
        download_mode = options.pop("download_mode")

        def callback(event, index, total, payload):
            if event == "start":
                if self._cancel_flag:
                    raise DownloadCancelled("用户取消了下载。")
                self._pause_event.wait()
                if self._cancel_flag:
                    raise DownloadCancelled("用户取消了下载。")

            with self._lock:
                if event == "start":
                    self.current = max(index - 1, 0)
                    self.total = total
                    self.current_url = payload["url"]
                    self.status = f"正在下载 {index}/{total}"
                    self.logs.append(f"[{index}/{total}] {payload['url']}")
                elif event == "item_progress":
                    self.status = payload.get("message", self.status)
                elif event == "log":
                    self.logs.append(payload["message"])
                elif event == "finish":
                    self.current = index
                    detail = payload.get("path") or payload.get("note", "")
                    self.logs.append(f"  -> {payload['status']}: {detail}")
                elif event == "summary":
                    self.current = total
                    self.total = total
                    self.running = False
                    self.status = "下载完成"
                    self.summary = payload
                    self.logs.append(json.dumps(payload, ensure_ascii=False, indent=2))

        try:
            if download_mode == "douyin_author_auto":
                run_author_batch(
                    source_url=options.pop("author_url"),
                    max_items=options.pop("max_items", 0),
                    date_start=options.pop("date_start", ""),
                    date_end=options.pop("date_end", ""),
                    progress_callback=callback,
                    **options,
                )
            elif download_mode == "douyin_media":
                run_note_batch(progress_callback=callback, **options)
            elif download_mode == "image":
                run_image_batch(progress_callback=callback, **options)
            else:
                run_share_batch(progress_callback=callback, **options)
        except DownloadCancelled:
            with self._lock:
                self.running = False
                self.paused = False
                self.status = "已取消"
                self.logs.append("下载已取消。")
        except Exception as exc:  # pragma: no cover - surfaced in UI
            with self._lock:
                self.running = False
                self.status = "下载失败"
                self.last_error = str(exc)
                self.logs.append(f"错误: {exc}")


STATE = DownloadState()


class AppHandler(BaseHTTPRequestHandler):
    server_version = "VideoDownloaderUI/1.0"

    def do_GET(self):
        if self.path == "/api/defaults":
            return self._json_response(STATE.defaults())
        if self.path == "/api/state":
            return self._json_response(STATE.snapshot())
        return self._serve_static()

    def do_POST(self):
        if self.path == "/api/start":
            payload = self._read_json_body()
            try:
                data = STATE.start(payload)
            except (ValueError, RuntimeError) as exc:
                return self._json_response({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return self._json_response(data)

        if self.path == "/api/pause":
            try:
                data = STATE.pause()
            except RuntimeError as exc:
                return self._json_response({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return self._json_response(data)

        if self.path == "/api/resume":
            try:
                data = STATE.resume()
            except RuntimeError as exc:
                return self._json_response({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return self._json_response(data)

        if self.path == "/api/cancel":
            try:
                data = STATE.cancel()
            except RuntimeError as exc:
                return self._json_response({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return self._json_response(data)

        if self.path == "/api/save-urls":
            payload = self._read_json_body()
            STATE.save_urls_text(str(payload.get("urls_text", "")))
            return self._json_response({"ok": True})

        return self._json_response({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else b"{}"
        return json.loads(body.decode("utf-8"))

    def _serve_static(self):
        route = self.path.split("?", 1)[0]
        if route == "/":
            route = "/index.html"

        file_path = (STATIC_DIR / route.lstrip("/")).resolve()
        if not str(file_path).startswith(str(STATIC_DIR.resolve())) or not file_path.exists():
            return self._json_response({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

        mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        content = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _json_response(self, payload, status=HTTPStatus.OK):
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, _format, *_args):
        return


def parse_args():
    parser = argparse.ArgumentParser(description="Start the local browser UI.")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind. Default: 127.0.0.1")
    parser.add_argument(
        "--port", type=int, default=8765, help="Preferred port to bind. Default: 8765"
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not auto-open the browser after the server starts.",
    )
    return parser.parse_args()


def is_port_free(host, port):
    with socket() as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) != 0


def choose_port(host, preferred_port):
    for port in range(preferred_port, preferred_port + 20):
        if is_port_free(host, port):
            return port
    raise OSError(f"无法找到可用端口，起始端口是 {preferred_port}")


def main():
    args = parse_args()
    port = choose_port(args.host, args.port)
    server = ThreadingHTTPServer((args.host, port), AppHandler)
    url = f"http://{args.host}:{port}"

    print("本地网页界面已启动。")
    print(f"打开地址: {url}")
    print("按 Ctrl+C 停止服务。")

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
