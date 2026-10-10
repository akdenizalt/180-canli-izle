def download_assets(assets: list[dict[str, Any]]) -> list[Path]:
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []

    for asset in assets:
        name = asset["name"]
        path = VIDEO_DIR / name
        partial = path.with_suffix(path.suffix + ".part")

        try:
            headers = {
                "Accept": "application/octet-stream",
                "User-Agent": "release-hls/1.0",
            }
            if TOKEN:
                headers["Authorization"] = f"Bearer {TOKEN}"

            req = urllib.request.Request(asset["url"], headers=headers)

            # Dosyayı RAM'e tamamen almadan, 1 MiB bloklarla indir.
            with urllib.request.urlopen(req, timeout=60) as response:
                with partial.open("wb") as output:
                    shutil.copyfileobj(response, output, length=1024 * 1024)

            size = partial.stat().st_size
            if size == 0:
                raise RuntimeError("indirilen dosya boş")

            expected_size = asset.get("size")
            if expected_size and size != expected_size:
                raise RuntimeError(
                    f"dosya boyutu eksik: {size}/{expected_size} bayt"
                )

            # İndirme tamamlanınca dosyayı son adına taşı.
            partial.replace(path)

            LOG.info("Asset indirildi: %s (%d bayt)", name, size)
            paths.append(path)

        except Exception as exc:
            partial.unlink(missing_ok=True)
            LOG.exception("Asset indirme başarısız: %s: %s", name, exc)
            raise RuntimeError(f"Asset indirilemedi: {name}") from exc

    return paths
