"""GitHub Release videolarından döngüsel HLS yayını üretir."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote


LOG = logging.getLogger("release-hls")

ROOT = Path(os.getenv("DATA_DIR", "/tmp/release-hls"))
SOURCE_DIR = ROOT / "source"
NORMALIZED_DIR = ROOT / "normalized"
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


def github_headers(
    accept: str = "application/vnd.github+json",
) -> dict[str, str]:
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
            "GITHUB_REPOSITORY 'sahip/repo' biçiminde ayarlanmalı."
        )

    url = (
        f"https://api.github.com/repos/{GITHUB_REPOSITORY}"
        "/releases/latest"
    )
    request = urllib.request.Request(url, headers=github_headers())

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            release = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise RuntimeError(
                "GitHub API 403 verdi. İstek sınırı dolmuş olabilir. "
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
    numbers = [number for number, _asset in programs]
    expected = list(range(1, len(numbers) + 1))

    if numbers != expected:
        raise RuntimeError(
            "Program numaraları program1.mp4'ten başlayıp kesintisiz "
            f"artmalı. Bulunan numaralar: {numbers or 'yok'}"
        )

    if not programs:
        missing.append("program1.mp4 (en az bir program videosu gerekli)")

    if missing:
        raise RuntimeError(
            "GitHub Release'te gerekli dosyalar eksik: "
            + ", ".join(missing)
        )

    ordered = [assets_by_name[name] for name in FIXED_ASSETS]
    ordered.extend(asset for _number, asset in programs)
    return ordered


def download_asset(asset: dict[str, Any]) -> Path:
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)

    name = asset["name"]
    destination = SOURCE_DIR / name
    partial = destination.with_suffix(destination.suffix + ".part")

    headers = {
        "Accept": "application/octet-stream",
        "User-Agent": "github-release-hls/1.0",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    request = urllib.request.Request(asset["url"], headers=headers)

    try:
        LOG.info("Asset indiriliyor: %s", name)

        # Videoyu RAM'e tamamen almadan, 1 MiB bloklarla diske yaz.
        with urllib.request.urlopen(request, timeout=120) as response:
            with partial.open("wb") as output:
                shutil.copyfileobj(
                    response,
                    output,
                    length=1024 * 1024,
                )

        size = partial.stat().st_size
        if size == 0:
            raise RuntimeError("indirilen dosya boş")

        expected_size = asset.get("size")
        if expected_size is not None and size != expected_size:
            raise RuntimeError(
                f"dosya boyutu eksik: {size}/{expected_size} bayt"
            )

        partial.replace(destination)
        LOG.info("Asset indirildi: %s (%d bayt)", name, size)
        return destination

    except Exception as exc:
        partial.unlink(missing_ok=True)
        LOG.exception("Asset indirme başarısız: %s: %s", name, exc)
        raise RuntimeError(f"Asset indirilemedi: {name}") from exc


def source_has_audio(source: Path) -> bool:
    command = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=index",
        "-of", "csv=p=0",
        str(source),
    ]

    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


def normalize_asset(source: Path, index: int) -> Path:
    NORMALIZED_DIR.mkdir(parents=True, exist_ok=True)
    destination = NORMALIZED_DIR / f"video_{index:04d}.mp4"
    partial = NORMALIZED_DIR / f"video_{index:04d}.partial.mp4"

    has_audio = source_has_audio(source)

    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel", "warning",
        "-i", str(source),
    ]

    if not has_audio:
        # Sessiz videolara da AAC ses akışı ekle; böylece tüm girişlerin
        # akış yapısı aynı olur.
        command.extend([
            "-f", "lavfi",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        ])

    command.extend([
        "-map", "0:v:0",
    ])

    if has_audio:
        command.extend([
            "-map", "0:a:0",
            "-af", "aresample=48000,apad",
        ])
    else:
        command.extend([
            "-map", "1:a:0",
        ])

    command.extend([
        "-vf",
        (
            "scale=640:360:force_original_aspect_ratio=decrease,"
            "pad=640:360:(ow-iw)/2:(oh-ih)/2,"
            "fps=20,format=yuv420p"
        ),
        "-r", "20",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-g", "80",
        "-keyint_min", "80",
        "-sc_threshold", "0",
        "-force_key_frames", "expr:gte(t,n_forced*4)",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "48000",
        "-ac", "2",
        "-shortest",
        "-video_track_timescale", "90000",
        "-movflags", "+faststart",
        str(partial),
    ])

    LOG.info("Video standart biçime dönüştürülüyor: %s", source.name)

    try:
        subprocess.run(command, check=True)
        if not partial.is_file() or partial.stat().st_size == 0:
            raise RuntimeError("dönüştürülmüş video boş oluştu")

        partial.replace(destination)
        LOG.info("Video hazır: %s", destination.name)

        # Ham dosyayı silerek geçici disk kullanımını azalt.
        source.unlink(missing_ok=True)
        return destination

    except Exception:
        partial.unlink(missing_ok=True)
        raise


def prepare_videos(assets: list[dict[str, Any]]) -> list[Path]:
    normalized: list[Path] = []

    for index, asset in enumerate(assets, start=1):
        if STOPPING.is_set():
            raise RuntimeError("Kapatma istendi; video hazırlama durduruldu.")

        source = download_asset(asset)
        normalized_path = normalize_asset(source, index)
        normalized.append(normalized_path)

    return normalized


def create_concat_file(video_paths: list[Path]) -> Path:
    ROOT.mkdir(parents=True, exist_ok=True)
    concat_file = ROOT / "inputs.txt"

    def quote_path(path: Path) -> str:
        value = str(path.resolve()).replace("\\", "/")
        return value.replace("'", "'\\''")

    contents = "".join(
        f"file '{quote_path(path)}'\n"
        for path in video_paths
    )
    concat_file.write_text(contents, encoding="utf-8")
    return concat_file


def start_ffmpeg(video_paths: list[Path]) -> subprocess.Popen[bytes]:
    HLS_DIR.mkdir(parents=True, exist_ok=True)
    concat_file = create_concat_file(video_paths)

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "info",
        "-re",
        "-stream_loop", "-1",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_file),
        "-map", "0:v:0",
        "-map", "0:a:0",
        "-c", "copy",
        "-f", "hls",
        "-hls_time", "4",
        "-hls_list_size", "6",
        "-hls_delete_threshold", "2",
        "-hls_flags",
        (
            "delete_segments+append_list+independent_segments+"
            "omit_endlist+temp_file"
        ),
        "-hls_segment_filename",
        str(HLS_DIR / "segment_%06d.ts"),
        str(PLAYLIST),
    ]

    LOG.info("FFmpeg HLS yayını başlatılıyor.")
    return subprocess.Popen(command)


def start_streaming() -> None:
    global FFMPEG, START_ERROR

    try:
        LOG.info("GitHub Release asset'leri alınıyor.")
        assets = get_release_assets()
        LOG.info("%d asset bulundu.", len(assets))

        video_paths = prepare_videos(assets)
        if STOPPING.is_set():
            return

        FFMPEG = start_ffmpeg(video_paths)
        return_code = FFMPEG.wait()

        if not STOPPING.is_set():
            START_ERROR = (
                "FFmpeg beklenmedik şekilde kapandı "
                f"(çıkış kodu {return_code})"
            )
            LOG.error(START_ERROR)

    except Exception as exc:
        START_ERROR = str(exc)
        LOG.exception("Yayın başlatılamadı: %s", exc)


class HLSRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.handle_request(send_body=True)

    def do_HEAD(self) -> None:
        self.handle_request(send_body=False)

    def handle_request(self, send_body: bool) -> None:
        path = unquote(self.path.split("?", 1)[0])

        if path == "/healthz":
            self.send_health(send_body)
            return

        if path == "/":
            self.send_player(send_body)
            return

        if path in ("/live", "/live/"):
            self.send_response(302)
            self.send_header("Location", "/live/index.m3u8")
            self.end_headers()
            return

        if path.startswith("/live/"):
            self.send_live_file(path, send_body)
            return

        self.send_error(404, "Not Found")

    def send_health(self, send_body: bool) -> None:
        ffmpeg_running = FFMPEG is not None and FFMPEG.poll() is None
        playlist_ready = PLAYLIST.is_file() and PLAYLIST.stat().st_size > 0
        healthy = (
            START_ERROR is None
            and ffmpeg_running
            and playlist_ready
        )

        body = json.dumps({
            "status": "ok" if healthy else "unhealthy",
            "ffmpeg_running": ffmpeg_running,
            "playlist_ready": playlist_ready,
            "error": START_ERROR,
        }).encode("utf-8")

        self.send_response(200 if healthy else 503)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

        if send_body:
            self.wfile.write(body)

    def send_player(self, send_body: bool) -> None:
        body = """<!doctype html>
<html lang="tr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Canlı Yayın</title>
  <style>
    body {
      margin: 24px;
      background: #111;
      color: #eee;
      font: 16px sans-serif;
      text-align: center;
    }
    video {
      width: min(100%, 960px);
      background: #000;
    }
  </style>
</head>
<body>
  <h1>Canlı Yayın</h1>
  <video id="video" controls playsinline></video>
  <p id="status">Yayın yükleniyor...</p>
  <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
  <script>
    const video = document.getElementById("video");
    const status = document.getElementById("status");
    const streamUrl = "/live/index.m3u8";

    if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = streamUrl;
      status.textContent = "Oynatmak için videoya bas.";
    } else if (window.Hls && Hls.isSupported()) {
      const hls = new Hls();
      hls.loadSource(streamUrl);
      hls.attachMedia(video);

      hls.on(Hls.Events.MANIFEST_PARSED, () => {
        status.textContent = "Oynatmak için videoya bas.";
      });

      hls.on(Hls.Events.ERROR, (_event, data) => {
        if (data.fatal) {
          status.textContent =
            "Yayın açılamadı. /healthz adresini kontrol et.";
        }
      });
    } else {
      status.textContent = "Bu tarayıcı HLS oynatmayı desteklemiyor.";
    }
  </script>
</body>
</html>""".encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

        if send_body:
            self.wfile.write(body)

    def send_live_file(self, url_path: str, send_body: bool) -> None:
        relative_path = url_path[len("/live/"):]
        live_root = HLS_DIR.resolve()
        requested_file = (HLS_DIR / relative_path).resolve()

        if (
            requested_file != live_root
            and live_root not in requested_file.parents
        ):
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

        size = requested_file.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(size))
        self.end_headers()

        if send_body:
            with requested_file.open("rb") as source:
                shutil.copyfileobj(
                    source,
                    self.wfile,
                    length=64 * 1024,
                )

    def log_message(self, fmt: str, *args: object) -> None:
        LOG.info("HTTP %s - %s", self.address_string(), fmt % args)


def main() -> int:
    global START_ERROR

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(
        ("0.0.0.0", port),
        HLSRequestHandler,
    )
    server.daemon_threads = True

    LOG.info("HTTP sunucusu %d portunda dinliyor.", port)

    def shutdown(_signum: int, _frame: object) -> None:
        STOPPING.set()
        threading.Thread(
            target=server.shutdown,
            daemon=True,
        ).start()

        if FFMPEG is not None and FFMPEG.poll() is None:
            FFMPEG.terminate()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    streamer = threading.Thread(
        target=start_streaming,
        daemon=True,
    )
    streamer.start()

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        STOPPING.set()

        if FFMPEG is not None and FFMPEG.poll() is None:
            FFMPEG.terminate()
            try:
                FFMPEG.wait(timeout=10)
            except subprocess.TimeoutExpired:
                FFMPEG.kill()

        server.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
