"""GitHub Release videolarından döngüsel HLS yayını üretir."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote


LOG = logging.getLogger("release-hls")

ROOT = Path(os.getenv("DATA_DIR", "/tmp/release-hls"))
VIDEO_DIR = ROOT / "videos"
HLS_DIR = ROOT / "live"
PLAYLIST = HLS_DIR / "index.m3u8"

FIXED_ASSETS = [
    "reklam1.mp4",
    "reklamlar.mp4",
    "reklam2.mp4",
    "jenerik.mp4",
]

PROGRAM_RE = re.compile(r"^program(\d+)\.mp4$", re.IGNORECASE)

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "").strip()

FFMPEG: subprocess.Popen[bytes] | None = None
START_ERROR: str | None = None
STOPPING = threading.Event()


def github_headers(accept: str = "application/vnd.github+json") -> dict[str, str]:
    headers = {
        "Accept": accept,
        "User-Agent": "github-release-hls/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def get_release_assets() -> list[dict[str, Any]]:
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", GITHUB_REPOSITORY):
        raise RuntimeError(
            "GITHUB_REPOSITORY ayarı 'sahip/repo' biçiminde olmalı."
        )

    url = (
        "https://api.github.com/repos/"
        f"{GITHUB_REPOSITORY}/releases/latest"
    )
    request = urllib.request.Request(url, headers=github_headers())

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            release = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise RuntimeError(
                "GitHub API 403 verdi. API rate limit dolmuş olabilir. "
                "Render'daki GITHUB_TOKEN değerini kontrol et."
            ) from exc
        raise RuntimeError(
            f"GitHub Releases API hatası ({exc.code}): {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GitHub API'ye ulaşılamadı: {exc}") from exc

    assets = release.get("assets", [])
    assets_by_name = {asset.get("name"): asset for asset in assets}

    missing = [
        name for name in FIXED_ASSETS
        if name not in assets_by_name
    ]

    programs: list[tuple[int, dict[str, Any]]] = []
    for asset in assets:
        match = PROGRAM_RE.fullmatch(asset.get("name", ""))
        if match:
            programs.append((int(match.group(1)), asset))

    programs.sort(key=lambda item: item[0])
    program_numbers = [number for number, _ in programs]
    expected_numbers = list(range(1, len(program_numbers) + 1))

    if program_numbers != expected_numbers:
        raise RuntimeError(
            "programN.mp4 dosyaları program1.mp4'ten başlayıp "
            f"kesintisiz artmalı. Bulunan numaralar: {program_numbers or 'yok'}"
        )

    if not programs:
        missing.append("program1.mp4 (en az bir program videosu gerekli)")

    if missing:
        raise RuntimeError(
            "GitHub Release'te gerekli asset'ler eksik: "
            + ", ".join(missing)
        )

    ordered_assets = [assets_by_name[name] for name in FIXED_ASSETS]
    ordered_assets.extend(asset for _, asset in programs)
    return ordered_assets


def download_assets(assets: list[dict[str, Any]]) -> list[Path]:
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    downloaded_paths: list[Path] = []

    for asset in assets:
        name = asset["name"]
        destination = VIDEO_DIR / name
        partial_path = destination.with_suffix(destination.suffix + ".part")

        headers = {
            "Accept": "application/octet-stream",
            "User-Agent": "github-release-hls/1.0",
        }
        if GITHUB_TOKEN:
            headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

        request = urllib.request.Request(asset["url"], headers=headers)

        try:
            LOG.info("Asset indiriliyor: %s", name)

            # Videoyu belleğe tamamen almadan, 1 MiB bloklarla diske yaz.
            with urllib.request.urlopen(request, timeout=120) as response:
                with partial_path.open("wb") as output:
                    shutil.copyfileobj(
                        response,
                        output,
                        length=1024 * 1024,
                    )

            actual_size = partial_path.stat().st_size
            if actual_size == 0:
                raise RuntimeError("indirilen dosya boş")

            expected_size = asset.get("size")
            if expected_size is not None and actual_size != expected_size:
                raise RuntimeError(
                    "indirilen dosyanın boyutu beklenenden farklı: "
                    f"{actual_size}/{expected_size} bayt"
                )

            partial_path.replace(destination)
            downloaded_paths.append(destination)

            LOG.info(
                "Asset indirildi: %s (%d bayt)",
                name,
                actual_size,
            )

        except Exception as exc:
            partial_path.unlink(missing_ok=True)
            LOG.exception("Asset indirme başarısız: %s: %s", name, exc)
            raise RuntimeError(f"Asset indirilemedi: {name}") from exc

    return downloaded_paths


def create_concat_file(video_paths: list[Path]) -> Path:
    ROOT.mkdir(parents=True, exist_ok=True)
    concat_path = ROOT / "inputs.txt"

    def quote_path(path: Path) -> str:
        escaped = str(path.resolve()).replace("\\", "/")
        escaped = escaped.replace("'", "'\\''")
        return escaped

    contents = "".join(
        f"file '{quote_path(path)}'\n"
        for path in video_paths
    )
    concat_path.write_text(contents, encoding="utf-8")
    return concat_path


def start_ffmpeg(video_paths: list[Path]) -> subprocess.Popen[bytes]:
    HLS_DIR.mkdir(parents=True, exist_ok=True)
    concat_path = create_concat_file(video_paths)

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "info",
        "-re",
        "-stream_loop",
        "-1",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_path),
        "-vf",
        (
            "scale=640:360:force_original_aspect_ratio=decrease,"
            "pad=640:360:(ow-iw)/2:(oh-ih)/2,"
            "fps=20"
        ),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-r",
        "20",
        "-g",
        "80",
        "-keyint_min",
        "80",
        "-sc_threshold",
        "0",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-f",
        "hls",
        "-hls_time",
        "4",
        "-hls_list_size",
        "6",
        "-hls_delete_threshold",
        "2",
        "-hls_flags",
        "delete_segments+append_list+independent_segments+omit_endlist",
        "-hls_segment_filename",
        str(HLS_DIR / "segment_%06d.ts"),
        str(PLAYLIST),
    ]

    LOG.info("FFmpeg başlatılıyor.")
    LOG.info("Girdi video sayısı: %d", len(video_paths))

    return subprocess.Popen(command)


class HLSRequestHandler(BaseHTTPRequestHandler):
    def do_HEAD(self) -> None:
        """Render ve istemcilerin HEAD sağlık kontrollerini destekle."""
        self._handle_request(send_body=False)

    def do_GET(self) -> None:
        self._handle_request(send_body=True)

    def _handle_request(self, send_body: bool) -> None:
        path = unquote(self.path.split("?", 1)[0])

        if path == "/healthz":
            self._send_health(send_body)
            return

        if path == "/":
            body = (
                b"GitHub Release HLS service. "
                b"Playlist: /live/index.m3u8\n"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if send_body:
                self.wfile.write(body)
            return

        if path == "/live" or path == "/live/":
            self.send_response(302)
            self.send_header("Location", "/live/index.m3u8")
            self.end_headers()
            return

        if path.startswith("/live/"):
            self._send_live_file(path, send_body)
            return

        self.send_error(404, "Not Found")

    def _send_health(self, send_body: bool) -> None:
        ffmpeg_running = FFMPEG is not None and FFMPEG.poll() is None
        healthy = START_ERROR is None and ffmpeg_running

        response = {
            "status": "ok" if healthy else "unhealthy",
            "ffmpeg_running": ffmpeg_running,
            "error": START_ERROR,
        }
        body = json.dumps(response).encode("utf-8")

        self.send_response(200 if healthy else 503)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

        if send_body:
            self.wfile.write(body)

    def _send_live_file(self, url_path: str, send_body: bool) -> None:
        relative_path = url_path[len("/live/"):]
        requested_file = (HLS_DIR / relative_path).resolve()
        live_root = HLS_DIR.resolve()

        if requested_file != live_root and live_root not in requested_file.parents:
            self.send_error(404, "Not Found")
            return

        if not requested_file.is_file():
            self.send_error(404, "HLS dosyası henüz hazır değil")
            return

        if requested_file.suffix == ".m3u8":
            content_type = "application/vnd.apple.mpegurl"
        elif requested_file.suffix == ".ts":
            content_type = "video/mp2t"
        else:
            content_type = "application/octet-stream"

        body = requested_file.read_bytes()

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

        if send_body:
            self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        LOG.info("HTTP %s - %s", self.address_string(), fmt % args)


def main() -> int:
    global FFMPEG, START_ERROR

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        LOG.info("GitHub Release asset'leri alınıyor.")
        assets = get_release_assets()
        LOG.info("Release asset sayısı: %d", len(assets))

        video_paths = download_assets(assets)
        FFMPEG = start_ffmpeg(video_paths)

    except Exception as exc:
        START_ERROR = str(exc)
        LOG.exception("Yayın başlatılamadı: %s", exc)

    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HLSRequestHandler)
    server.daemon_threads = True

    LOG.info("HTTP sunucusu %d portunda dinliyor.", port)

    def handle_shutdown(_signum: int, _frame: object) -> None:
        STOPPING.set()
        threading.Thread(
            target=server.shutdown,
            daemon=True,
        ).start()

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    if FFMPEG is not None:
        def monitor_ffmpeg() -> None:
            global START_ERROR

            return_code = FFMPEG.wait()
            if not STOPPING.is_set():
                START_ERROR = (
                    "FFmpeg beklenmedik şekilde kapandı "
                    f"(çıkış kodu {return_code})"
                )
                LOG.error(START_ERROR)

        threading.Thread(
            target=monitor_ffmpeg,
            daemon=True,
        ).start()

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        if FFMPEG is not None and FFMPEG.poll() is None:
            FFMPEG.terminate()
            try:
                FFMPEG.wait(timeout=10)
            except subprocess.TimeoutExpired:
                FFMPEG.kill()

        server.server_close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
