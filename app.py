"""GitHub Release assets'i döngüsel HLS yayınına dönüştürür."""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

LOG = logging.getLogger("release-hls")
ROOT = Path(os.getenv("DATA_DIR", "/tmp/release-hls"))
VIDEO_DIR = ROOT / "videos"
HLS_DIR = ROOT / "live"
PLAYLIST = HLS_DIR / "index.m3u8"
FIXED_ASSETS = ["reklam1.mp4", "reklamlar.mp4", "reklam2.mp4", "jenerik.mp4"]
PROGRAM_RE = re.compile(r"^program(\d+)\.mp4$", re.IGNORECASE)
TOKEN = os.getenv("GITHUB_TOKEN")
REPOSITORY = os.getenv("GITHUB_REPOSITORY", "").strip()
FFMPEG: subprocess.Popen[bytes] | None = None
START_ERROR: str | None = None
STOP = threading.Event()


def request(url: str, *, accept: str = "application/vnd.github+json") -> Any:
    headers = {"Accept": accept, "User-Agent": "release-hls/1.0", "X-GitHub-Api-Version": "2022-11-28"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as response:
        return response.read()


def get_assets() -> list[dict[str, Any]]:
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", REPOSITORY):
        raise RuntimeError("GITHUB_REPOSITORY 'sahip/repo' biçiminde ayarlanmalı.")
    api = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
    try:
        release = json.loads(request(api))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GitHub Releases API hatası ({exc.code}): {exc.reason}") from exc
    assets = release.get("assets", [])
    by_name = {a.get("name"): a for a in assets}
    missing = [name for name in FIXED_ASSETS if name not in by_name]
    programs: list[tuple[int, dict[str, Any]]] = []
    for asset in assets:
        match = PROGRAM_RE.fullmatch(asset.get("name", ""))
        if match:
            programs.append((int(match.group(1)), asset))
    programs.sort(key=lambda entry: entry[0])
    numbers = [number for number, _ in programs]
    expected = list(range(1, len(numbers) + 1))
    if numbers != expected:
        raise RuntimeError(f"programN.mp4 numaraları program1'den kesintisiz artmalı; bulunan: {numbers or 'yok'}")
    if not programs:
        missing.append("program1.mp4 (en az bir program dosyası gerekli)")
    if missing:
        raise RuntimeError("Release asset'leri eksik: " + ", ".join(missing))
    return [by_name[name] for name in FIXED_ASSETS] + [asset for _, asset in programs]


def download_assets(assets: list[dict[str, Any]]) -> list[Path]:
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for asset in assets:
        name = asset["name"]
        path = VIDEO_DIR / name
        try:
            data = request(asset["url"], accept="application/octet-stream")
            if not data:
                raise RuntimeError("indirilen dosya boş")
            path.write_bytes(data)
            if path.stat().st_size == 0:
                raise RuntimeError("indirilen dosya boş")
            LOG.info("Asset indirildi: %s (%d bayt)", name, path.stat().st_size)
            paths.append(path)
        except Exception as exc:
            LOG.exception("Asset indirme başarısız: %s: %s", name, exc)
            raise RuntimeError(f"Asset indirilemedi: {name}") from exc
    return paths


def start_ffmpeg(paths: list[Path]) -> subprocess.Popen[bytes]:
    HLS_DIR.mkdir(parents=True, exist_ok=True)
    concat = ROOT / "inputs.txt"
    # concat demuxer accepts quoted paths; apostrophes are escaped per ffmpeg syntax.
    def quote(path: Path) -> str:
        return str(path).replace("\\", "/").replace("'", "'\\''")
    concat.write_text("".join(f"file '{quote(path)}'\n" for path in paths), encoding="utf-8")
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "info", "-re", "-stream_loop", "-1",
        "-f", "concat", "-safe", "0", "-i", str(concat), "-vf", "scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2,fps=20",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-r", "20", "-g", "80", "-keyint_min", "80", "-sc_threshold", "0",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2", "-f", "hls", "-hls_time", "4", "-hls_list_size", "6",
        "-hls_delete_threshold", "2", "-hls_flags", "delete_segments+append_list+independent_segments+omit_endlist",
        "-hls_segment_filename", str(HLS_DIR / "segment_%06d.ts"), str(PLAYLIST),
    ]
    LOG.info("FFmpeg başlatılıyor: %s", " ".join(command))
    return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=None)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            healthy = START_ERROR is None and FFMPEG is not None and FFMPEG.poll() is None
            body = json.dumps({"status": "ok" if healthy else "unhealthy", "ffmpeg_running": bool(FFMPEG and FFMPEG.poll() is None), "error": START_ERROR}).encode()
            self.send_response(200 if healthy else 503)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/live/index.m3u8" or self.path.startswith("/live/"):
            relative = self.path.removeprefix("/live/").split("?", 1)[0]
            candidate = (HLS_DIR / relative).resolve()
            if HLS_DIR.resolve() not in candidate.parents and candidate != HLS_DIR.resolve():
                self.send_error(404)
                return
            if not candidate.is_file():
                self.send_error(404)
                return
            content_type = "application/vnd.apple.mpegurl" if candidate.suffix == ".m3u8" else "video/mp2t"
            body = candidate.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def log_message(self, fmt: str, *args: object) -> None:
        LOG.info("HTTP %s - %s", self.address_string(), fmt % args)


def main() -> int:
    global FFMPEG, START_ERROR
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")
    try:
        assets = get_assets()
        paths = download_assets(assets)
        FFMPEG = start_ffmpeg(paths)
    except Exception as exc:
        START_ERROR = str(exc)
        LOG.exception("Yayın başlatılamadı: %s", exc)
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    LOG.info("HTTP sunucusu :%d üzerinde dinliyor", port)

    def shutdown(_signum: int, _frame: object) -> None:
        STOP.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    monitor: threading.Thread | None = None
    if FFMPEG:
        def watch() -> None:
            global START_ERROR
            code = FFMPEG.wait()
            if not STOP.is_set():
                START_ERROR = f"FFmpeg beklenmedik şekilde kapandı (çıkış kodu {code})"
                LOG.error(START_ERROR)
        monitor = threading.Thread(target=watch, daemon=True)
        monitor.start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        if FFMPEG and FFMPEG.poll() is None:
            FFMPEG.terminate()
            try:
                FFMPEG.wait(timeout=10)
            except subprocess.TimeoutExpired:
                FFMPEG.kill()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
