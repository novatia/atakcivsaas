"""Mappa offline HD da un ordine SkyFi: GeoTIFF originale → GeoPackage → data package.

Il data package «online» del tab SkyFi contiene solo l'XML che punta ai tile
WMTS di SkyFi (tilesUrl): pesa pochi KB e ATAK mostra quello che SkyFi serve
in streaming, che allo zoom massimo è più sgranato dell'immagine originale.
Qui invece si parte dal deliverable GeoTIFF vero (view-ready, altrimenti COG)
e lo si converte con GDAL in un GeoPackage raster a tile EPSG:3857
(GoogleMapsCompatible, la griglia di ATAK), che funziona anche senza rete.

Pipeline (comandi GDAL di sistema, pacchetto `gdal-bin`):

1. `gdalinfo -json -approx_stats` sul sorgente → bande, tipo, nodata, statistiche
2. `gdalwarp -of VRT` in 3857, una prima volta per sapere la risoluzione
   nativa, poi con `-tr` pari alla risoluzione ESATTA del livello di zoom
   subito più fine (o del tetto scelto) e `-tap`: i pixel cadono sulla
   griglia delle tile, così il ricampionamento (cubico) avviene una volta sola
3. `gdal_translate -of GPKG` → solo RGB + alfa, 16 bit riportati a 8 con uno
   stretch media ± 2,5σ per banda, tile JPEG (PNG solo ai bordi trasparenti)
4. `gdaladdo -r average` → livelli di zoom inferiori, fino a una sola tile

Il modulo non conosce Flask né il DB: scaricare, registrare il DataPackage e
tenere lo stato del job è compito dell'app.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from typing import Callable

GDAL_TOOLS = ("gdalinfo", "gdal_translate", "gdalwarp", "gdaladdo")

# Preferenza fra i deliverable: view-ready è già a 8 bit RGB «da vedere»;
# il COG può essere a 16 bit o multispettrale e va riscalato
SOURCE_FIELDS = {
    "view-ready": ("downloadViewReadyCogUrl", "viewReadyCogSize"),
    "cog": ("downloadCogUrl", "cogSize"),
}

JPEG_QUALITY = 85
# Stretch dei raster non a 8 bit: media ± K deviazioni standard per banda
STRETCH_SIGMA = 2.5

# Circonferenza equatoriale in EPSG:3857 e lato delle tile
WORLD_METERS = 2 * math.pi * 6378137
TILE_SIZE = 256
MAX_ZOOM = 22  # ATAK non va oltre

# Spazio libero richiesto rispetto alla dimensione del sorgente: il file
# scaricato + GeoPackage (con JPEG di solito più piccolo del sorgente) + zip
DISK_FACTOR = 3

# DataPackage.size di OTS è un Integer (32 bit su PostgreSQL)
MAX_PACKAGE_BYTES = 2**31 - 1


class OfflineMapError(RuntimeError):
    """Conversione impossibile: GDAL assente, sorgente illeggibile, comando fallito."""


def missing_tools() -> list[str]:
    return [tool for tool in GDAL_TOOLS if shutil.which(tool) is None]


def pick_source(order: dict) -> tuple[str, int] | None:
    """(tipo di deliverable, dimensione dichiarata) da usare per l'ordine."""
    for kind, (url_key, size_key) in SOURCE_FIELDS.items():
        if order.get(url_key):
            return kind, int(order.get(size_key) or 0)
    return None


# ----------------------------------------------------------------------
# Livelli di zoom
# ----------------------------------------------------------------------


def zoom_resolution(zoom: int) -> float:
    """Metri per pixel (EPSG:3857) di un livello di zoom."""
    return WORLD_METERS / TILE_SIZE / 2**zoom


def native_zoom(resolution: float) -> int:
    """Primo livello con pixel più piccoli o uguali a quelli del sorgente
    (come ZOOM_LEVEL_STRATEGY=UPPER): nessun dettaglio perso. Il margine
    evita di salire di un livello — 4 volte i dati — per un arrotondamento."""
    if resolution <= 0:
        raise OfflineMapError("risoluzione del sorgente non valida")
    zoom = math.ceil(math.log2(WORLD_METERS / TILE_SIZE / resolution) - 0.01)
    return max(0, min(zoom, MAX_ZOOM))


# ----------------------------------------------------------------------
# Argomenti dei comandi GDAL
# ----------------------------------------------------------------------


def _band_interp(band: dict) -> str:
    return (band.get("colorInterpretation") or "").lower()


def _num(value) -> str:
    return f"{float(value):.10g}"


def _alpha_band(info: dict) -> int | None:
    return next((b["band"] for b in info.get("bands") or [] if _band_interp(b) == "alpha"), None)


def color_bands(info: dict) -> list[dict]:
    """Bande da tenere: rosso/verde/blu se dichiarate, altrimenti le prime
    tre (multispettrali senza colorInterpretation), altrimenti una grigia."""
    bands = info.get("bands") or []
    if not bands:
        raise OfflineMapError("il GeoTIFF non ha bande raster")
    alpha = _alpha_band(info)
    color = [b for b in bands if b["band"] != alpha]
    if not color:
        raise OfflineMapError("il GeoTIFF ha solo la banda alfa")
    by_interp = {_band_interp(b): b for b in color}
    if all(k in by_interp for k in ("red", "green", "blue")):
        return [by_interp["red"], by_interp["green"], by_interp["blue"]]
    return color[:3] if len(color) >= 3 else color[:1]


def _nodata(info: dict):
    return next((b["noDataValue"] for b in info.get("bands") or [] if b.get("noDataValue") is not None), None)


def warp_args(info: dict, resolution: float | None = None) -> list[str]:
    """gdalwarp verso un VRT in 3857 con banda alfa di uscita (bordi della
    riproiezione e nodata trasparenti invece che neri)."""
    args = [
        "-of", "VRT", "-overwrite",
        "-t_srs", "EPSG:3857",
        "-r", "cubic",
        "-dstalpha",
        "-wo", "DST_ALPHA_MAX=255",
        "-wo", "NUM_THREADS=ALL_CPUS",
    ]
    nodata = _nodata(info)
    if nodata is not None and _alpha_band(info) is None:
        args += ["-srcnodata", _num(nodata)]
    if resolution:
        args += ["-tr", _num(resolution), _num(resolution), "-tap"]
    return args


def translate_args(info: dict, quality: int = JPEG_QUALITY) -> list[str]:
    """gdal_translate dal VRT riproiettato al GeoPackage. Nel VRT le bande
    non-alfa del sorgente mantengono la loro numerazione e l'alfa creata da
    -dstalpha è l'ultima."""
    chosen = color_bands(info)
    alpha_source = _alpha_band(info)
    warped_alpha = len(info["bands"]) + (0 if alpha_source else 1)

    args = ["-of", "GPKG"]
    for b in chosen:
        args += ["-b", str(b["band"])]
    args += ["-b", str(warped_alpha)]
    args += ["-colorinterp", "red,green,blue,alpha" if len(chosen) == 3 else "gray,alpha"]

    if any((b.get("type") or "").lower() != "byte" for b in chosen):
        args += ["-ot", "Byte"]
        for i, b in enumerate(chosen, start=1):
            low, high = stretch(b)
            args += [f"-scale_{i}", _num(low), _num(high), "0", "255"]
        # L'alfa di gdalwarp ha già massimo 255 (DST_ALPHA_MAX)
        args += [f"-scale_{len(chosen) + 1}", "0", "255", "0", "255"]

    args += [
        "-co", "TILING_SCHEME=GoogleMapsCompatible",
        # Il VRT è già sulla risoluzione esatta del livello: AUTO lo prende
        # così com'è, senza un secondo ricampionamento
        "-co", "ZOOM_LEVEL_STRATEGY=AUTO",
        "-co", "RESAMPLING=CUBIC",
        "-co", "TILE_FORMAT=AUTO",
        "-co", f"QUALITY={int(quality)}",
        "--config", "GDAL_NUM_THREADS", "ALL_CPUS",
    ]
    return args


def stretch(band: dict) -> tuple[float, float]:
    mean, std = band.get("mean"), band.get("stdDev")
    low_lim, high_lim = band.get("minimum"), band.get("maximum")
    if mean is None or std is None:
        if low_lim is None or high_lim is None:
            raise OfflineMapError(f"banda {band.get('band')} senza statistiche: impossibile riscalarla a 8 bit")
        return float(low_lim), float(high_lim)
    low, high = mean - STRETCH_SIGMA * std, mean + STRETCH_SIGMA * std
    if low_lim is not None:
        low = max(low, float(low_lim))
    if high_lim is not None:
        high = min(high, float(high_lim))
    if high <= low:
        high = low + 1
    return low, high


def overview_factors(width: int, height: int, tile: int = TILE_SIZE) -> list[str]:
    """2, 4, 8, … finché il livello più piccolo sta in una tile."""
    factors, f = [], 2
    while max(width, height) / (f // 2) > tile:
        factors.append(str(f))
        f *= 2
    return factors


# ----------------------------------------------------------------------
# Esecuzione
# ----------------------------------------------------------------------

# Barra di GDAL: «0...10...20...30...40...50...60...70...80...90...100 - done.»
_PROGRESS_RE = re.compile(r"(\d{1,3})(?=\.\.\.|\s*-\s*done)")


def run(cmd: list[str], on_progress: Callable[[float], None] | None = None, timeout: int = 6 * 3600) -> str:
    """Esegue un comando GDAL leggendo la barra di avanzamento dallo stdout.
    Ritorna l'output; solleva OfflineMapError se il comando fallisce."""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as e:
        raise OfflineMapError(f"{cmd[0]} non eseguibile: {e}") from e
    output, tail = [], ""
    deadline = time.monotonic() + timeout
    fd = proc.stdout.fileno()
    try:
        # os.read restituisce quello che c'è già: la barra di GDAL arriva a
        # pezzi senza a capo, una read(n) bufferizzata aspetterebbe la fine.
        # Dentro OTS (eventlet/gevent) il pipe è non bloccante: senza dati
        # os.read solleva EAGAIN invece di aspettare, e si riprova dopo una
        # pausa (time.sleep lì è cooperativo, non blocca il server)
        while True:
            if time.monotonic() > deadline:
                raise subprocess.TimeoutExpired(cmd, timeout)
            try:
                raw = os.read(fd, 4096)
            except BlockingIOError:
                time.sleep(0.2)
                continue
            if not raw:
                break
            chunk = raw.decode("utf-8", "replace")
            output.append(chunk)
            if on_progress:
                tail = (tail + chunk)[-200:]
                values = _PROGRESS_RE.findall(tail)
                if values:
                    on_progress(min(int(values[-1]), 100) / 100)
        proc.wait(timeout=max(1.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        proc.kill()
        raise OfflineMapError(f"{cmd[0]} oltre il tempo massimo") from None
    text = "".join(output)
    if proc.returncode != 0:
        last = "\n".join(line for line in text.splitlines() if line.strip())[-800:]
        raise OfflineMapError(f"{cmd[0]} fallito (codice {proc.returncode}): {last}")
    return text


def gdalinfo(path: str, stats: bool = False) -> dict:
    text = run(["gdalinfo", "-json", *(["-approx_stats"] if stats else []), path])
    try:
        return json.loads(text[text.index("{"):])
    except ValueError as e:
        raise OfflineMapError(f"gdalinfo: risposta non leggibile ({e})") from e


def pixel_size(info: dict) -> float:
    gt = info.get("geoTransform") or []
    if len(gt) < 6:
        raise OfflineMapError("il raster non è georeferenziato")
    return (abs(gt[1]) + abs(gt[5])) / 2


def convert(
    source: str,
    gpkg: str,
    work_dir: str,
    on_step: Callable[[str, float], None] | None = None,
    max_zoom: int | None = None,
    quality: int = JPEG_QUALITY,
) -> dict:
    """GeoTIFF → GeoPackage. `on_step(fase, frazione)` riceve l'avanzamento;
    `max_zoom` limita il livello più dettagliato (None = nativo).
    Ritorna {native_zoom, zoom, resolution, width, height}."""
    step = on_step or (lambda phase, fraction: None)

    step("analisi", 0)
    info = gdalinfo(source, stats=True)
    if not info.get("coordinateSystem", {}).get("wkt"):
        raise OfflineMapError("il GeoTIFF non ha un sistema di riferimento")

    vrt = os.path.join(work_dir, "warped.vrt")
    run(["gdalwarp", *warp_args(info), source, vrt])
    native = native_zoom(pixel_size(gdalinfo(vrt)))
    zoom = min(native, max_zoom) if max_zoom is not None else native
    resolution = zoom_resolution(zoom)
    run(["gdalwarp", *warp_args(info, resolution), source, vrt])

    step("conversione", 0)
    if os.path.exists(gpkg):
        os.remove(gpkg)
    run(["gdal_translate", *translate_args(info, quality), vrt, gpkg], lambda f: step("conversione", f))

    out = gdalinfo(gpkg)
    width, height = (out.get("size") or [0, 0])[:2]
    factors = overview_factors(int(width), int(height))
    step("livelli di zoom", 0)
    if factors:
        run(["gdaladdo", "-r", "average", gpkg, *factors], lambda f: step("livelli di zoom", f))
    return {"native_zoom": native, "zoom": zoom, "resolution": resolution, "width": width, "height": height}


# ----------------------------------------------------------------------
# Data package
# ----------------------------------------------------------------------


def manifest_xml(package_name: str, package_uid: str, entry: str) -> bytes:
    root = ET.Element("MissionPackageManifest", {"version": "2"})
    config = ET.SubElement(root, "Configuration")
    ET.SubElement(config, "Parameter", {"name": "uid", "value": package_uid})
    ET.SubElement(config, "Parameter", {"name": "name", "value": package_name})
    contents = ET.SubElement(root, "Contents")
    ET.SubElement(contents, "Content", {"ignore": "false", "zipEntry": entry})
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode").encode("utf-8")


def build_package(gpkg: str, zip_path: str, package_name: str, map_name: str) -> tuple[str, int]:
    """Zip TAK con manifest + GeoPackage (STORED: le tile sono già JPEG/PNG,
    ricomprimerle costa solo CPU). Ritorna (sha256, dimensione)."""
    package_uid = str(uuid.uuid4())
    entry = f"{package_uid}/{map_name}.gpkg"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        zf.writestr("MANIFEST/manifest.xml", manifest_xml(package_name, package_uid, entry))
        zf.write(gpkg, entry, compress_type=zipfile.ZIP_STORED)

    sha256 = hashlib.sha256()
    with open(zip_path, "rb") as f:
        while chunk := f.read(4 * 1024 * 1024):
            sha256.update(chunk)
    return sha256.hexdigest(), os.path.getsize(zip_path)
