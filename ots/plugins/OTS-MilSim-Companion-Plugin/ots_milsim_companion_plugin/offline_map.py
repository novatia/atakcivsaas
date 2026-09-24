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
   griglia delle tile, così il ricampionamento (cubico) avviene una volta sola.
   Se il sorgente è già su quella griglia (i deliverable SkyFi lo sono:
   EPSG:3857, pixel dello zoom 20) si usa `near`: pixel copiati identici
3. `gdal_translate -of GPKG` → solo RGB + alfa, 16 bit riportati a 8 con uno
   stretch media ± 2,5σ per banda, tile PNG senza perdita (default) o JPEG
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

GDAL_TOOLS = ("gdalinfo", "gdal_translate", "gdalwarp", "gdaladdo", "gdaldem")

# Prodotti: «visible» è l'immagine a colori; «vegetation» la mappa dello stato
# della vegetazione (NDVI) calcolata dalla banda infrarossa del COG
PRODUCTS = ("visible", "vegetation")
VEGETATION_SCALES = ("relativa", "assoluta")

# Tavolozza NDVI → colore (punti di controllo interpolati da gdaldem). La scala
# «assoluta» usa questi valori; la «relativa» ridistribuisce i punti dalla
# vegetazione in su (>= VEG_FIRST_STOP) fra il 2° e il 98° percentile della
# scena: a inizio aprile un bosco in foglia nuova ha NDVI 0,2-0,4 e in scala
# assoluta sembrerebbe «sofferente» (ordine 2639L3JY, Emilia-Romagna).
VEGETATION_STOPS = [
    (-0.30, (30, 70, 160)),    # acqua
    (-0.02, (90, 150, 210)),
    (0.00, (120, 90, 70)),     # suolo nudo, strade, costruito
    (0.12, (175, 120, 80)),
    (0.22, (215, 90, 50)),     # vegetazione scarsa o poco attiva
    (0.32, (245, 165, 60)),
    (0.42, (250, 225, 90)),    # vegetazione rada o intermedia
    (0.52, (190, 225, 90)),
    (0.62, (110, 190, 70)),    # vegetazione fitta
    (0.72, (40, 140, 50)),
    (0.85, (10, 80, 30)),      # la più fitta e vigorosa
]
VEG_FIRST_STOP = 0.12
VEGETATION_LEGEND = [
    ((30, 70, 160), "acqua (anche tetti scuri e ombre profonde)"),
    ((120, 90, 70), "suolo nudo, strade, costruito"),
    ((215, 90, 50), "vegetazione scarsa o poco attiva"),
    ((250, 225, 90), "vegetazione rada o intermedia"),
    ((110, 190, 70), "vegetazione fitta"),
    ((10, 80, 30), "vegetazione la più fitta e vigorosa"),
]

# Preferenza fra i deliverable: il COG è l'immagine originale a piena qualità
# (di solito 16 bit, 4 bande: va riscalata a 8 bit), il view-ready è una
# versione già compressa da SkyFi (ordine 26383Z2P, 3 km²: COG 1,1 GB contro
# view-ready 92 MB) e ricomprimerlo in JPEG toglie altro dettaglio
SOURCE_FIELDS = {
    "cog": ("downloadCogUrl", "cogSize"),
    "view-ready": ("downloadViewReadyCogUrl", "viewReadyCogSize"),
}

# Formato delle tile del GeoPackage: (TILE_FORMAT di GDAL, qualità JPEG)
TILE_FORMATS = {
    "png": ("PNG", None),  # senza perdita: 3-5 volte più grande del JPEG
    "jpeg95": ("AUTO", 95),
    "jpeg85": ("AUTO", 85),  # compatto; AUTO = PNG solo ai bordi trasparenti
}
# «geotiff» (default): niente tile, un GeoTIFF COG (DEFLATE, senza perdita,
# overview interne) che ATAK apre come immagine nativa con GDAL. Il
# GeoPackage PNG dell'ordine 26383Z2P aveva lo zoom 20 completo (3715 tile),
# ma ATAK-CIV lo mostrava a blocchi da ~0,9 m, cioè allo zoom 17; il
# GeoTIFF dello stesso COG si vede al dettaglio pieno (verificato in campo).
FORMATS = ("geotiff", *TILE_FORMATS)
DEFAULT_FORMAT = "geotiff"
FORMAT_EXTENSIONS = {"geotiff": ".tif"}  # gli altri: .gpkg

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


def available_sources(order: dict) -> dict:
    """{tipo di deliverable: dimensione dichiarata} dei GeoTIFF dell'ordine."""
    return {
        kind: int(order.get(size_key) or 0)
        for kind, (url_key, size_key) in SOURCE_FIELDS.items()
        if order.get(url_key)
    }


def pick_source(order: dict, prefer: str | None = None) -> tuple[str, int] | None:
    """(tipo di deliverable, dimensione dichiarata) da usare per l'ordine:
    `prefer` se c'è, altrimenti il primo di SOURCE_FIELDS (il COG)."""
    sources = available_sources(order)
    if prefer in sources:
        return prefer, sources[prefer]
    return next(iter(sources.items()), None)


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


def aligned_zoom(info: dict) -> int | None:
    """Livello di zoom su cui il sorgente è GIÀ allineato (EPSG:3857, pixel
    della risoluzione esatta del livello, origine su un pixel intero della
    griglia delle tile), altrimenti None. È il caso dei deliverable SkyFi:
    lì si copiano i pixel così come sono, senza ricampionare."""
    wkt = (info.get("coordinateSystem") or {}).get("wkt") or ""
    if "3857" not in wkt and "Pseudo-Mercator" not in wkt:
        return None
    gt = info.get("geoTransform") or []
    if len(gt) < 6 or gt[2] or gt[4] or gt[1] <= 0 or gt[5] >= 0 or abs(abs(gt[5]) - gt[1]) > gt[1] * 1e-9:
        return None
    zoom = native_zoom(gt[1])
    res = zoom_resolution(zoom)
    if abs(gt[1] - res) > res * 1e-9:
        return None
    col, row = (gt[0] + WORLD_METERS / 2) / res, (WORLD_METERS / 2 - gt[3]) / res
    if abs(col - round(col)) > 1e-3 or abs(row - round(row)) > 1e-3:
        return None
    return zoom


def warp_args(info: dict, resolution: float | None = None, resampling: str = "cubic") -> list[str]:
    """gdalwarp verso un VRT in 3857 con banda alfa di uscita (bordi della
    riproiezione e nodata trasparenti invece che neri)."""
    args = [
        "-of", "VRT", "-overwrite",
        "-t_srs", "EPSG:3857",
        "-r", resampling,
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


def translate_args(info: dict, tile_format: str = "png") -> list[str]:
    """gdal_translate dal VRT riproiettato al GeoPackage. Nel VRT le bande
    non-alfa del sorgente mantengono la loro numerazione e l'alfa creata da
    -dstalpha è l'ultima. `tile_format` è una chiave di TILE_FORMATS."""
    if tile_format not in TILE_FORMATS:
        raise OfflineMapError(f"formato non valido: {tile_format}")
    gdal_format, quality = TILE_FORMATS[tile_format]
    return ["-of", "GPKG", *band_args(info), *[
        "-co", "TILING_SCHEME=GoogleMapsCompatible",
        # Il VRT è già sulla risoluzione esatta del livello: AUTO lo prende
        # così com'è, senza un secondo ricampionamento
        "-co", "ZOOM_LEVEL_STRATEGY=AUTO",
        "-co", "RESAMPLING=CUBIC",
        "-co", f"TILE_FORMAT={gdal_format}",
        *(["-co", f"QUALITY={quality}"] if quality else []),
        "--config", "GDAL_NUM_THREADS", "ALL_CPUS",
    ]]


def geotiff_args(info: dict) -> list[str]:
    """gdal_translate dal VRT riproiettato a un GeoTIFF COG senza perdita:
    stesse bande del GeoPackage, DEFLATE con predictor, overview interne
    (il driver COG le crea da solo) per gli zoom bassi."""
    return ["-of", "COG", *band_args(info), *[
        "-co", "COMPRESS=DEFLATE",
        "-co", "PREDICTOR=2",
        "-co", "BIGTIFF=IF_SAFER",
        "-co", "OVERVIEW_RESAMPLING=AVERAGE",
        "-co", "NUM_THREADS=ALL_CPUS",
        "--config", "GDAL_NUM_THREADS", "ALL_CPUS",
    ]]


def band_args(info: dict) -> list[str]:
    """Selezione RGB(+alfa) e, se non a 8 bit, stretch a Byte."""
    chosen = color_bands(info)
    alpha_source = _alpha_band(info)
    warped_alpha = len(info["bands"]) + (0 if alpha_source else 1)

    args = []
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
        # Cache dei blocchi di GDAL limitata: sul sorgente da 1,2 GB di SkyFi
        # gdal_translate arrivava a 2,5 GB di RAM col default (5% della RAM,
        # più i buffer), sul server convive con OTS e PostgreSQL
        env = {**os.environ, "GDAL_CACHEMAX": os.environ.get("GDAL_CACHEMAX", "512")}
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
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
    tile_format: str = DEFAULT_FORMAT,
) -> dict:
    """GeoTIFF → GeoPackage (o GeoTIFF COG con tile_format="geotiff": allora
    `gpkg` è il percorso del .tif). `on_step(fase, frazione)` riceve
    l'avanzamento; `max_zoom` limita il livello più dettagliato (None = nativo).
    Ritorna {native_zoom, zoom, resolution, ground_resolution, aligned,
    resampling, tile_format, width, height}."""
    step = on_step or (lambda phase, fraction: None)
    if tile_format not in FORMATS:
        raise OfflineMapError(f"formato non valido: {tile_format}")

    step("analisi", 0)
    info = gdalinfo(source, stats=True)
    if not info.get("coordinateSystem", {}).get("wkt"):
        raise OfflineMapError("il GeoTIFF non ha un sistema di riferimento")

    vrt = os.path.join(work_dir, "warped.vrt")
    run(["gdalwarp", *warp_args(info), source, vrt])
    first = gdalinfo(vrt)
    native = native_zoom(pixel_size(first))
    ground = pixel_size(first) * math.cos(math.radians(center_lat(first)))
    zoom = min(native, max_zoom) if max_zoom is not None else native
    resolution = zoom_resolution(zoom)
    # Sorgente già sulla griglia del livello scelto: «near» copia i pixel
    # identici (con cubic sarebbero comunque quasi uguali, ma non bit a bit)
    aligned = aligned_zoom(info) == zoom
    resampling = "near" if aligned else "cubic"
    run(["gdalwarp", *warp_args(info, resolution, resampling), source, vrt])

    step("conversione", 0)
    if os.path.exists(gpkg):
        os.remove(gpkg)
    args = geotiff_args(info) if tile_format == "geotiff" else translate_args(info, tile_format)
    run(["gdal_translate", *args, vrt, gpkg], lambda f: step("conversione", f))

    out = gdalinfo(gpkg)
    width, height = (out.get("size") or [0, 0])[:2]
    factors = overview_factors(int(width), int(height)) if tile_format != "geotiff" else []
    if factors:
        step("livelli di zoom", 0)
        run(["gdaladdo", "-r", "average", gpkg, *factors], lambda f: step("livelli di zoom", f))
    return {
        "native_zoom": native,
        "zoom": zoom,
        "resolution": resolution,
        "ground_resolution": ground,
        "aligned": aligned,
        "resampling": resampling,
        "tile_format": tile_format,
        "width": width,
        "height": height,
    }


# ----------------------------------------------------------------------
# Stato della vegetazione (NDVI dalla banda infrarossa)
# ----------------------------------------------------------------------


def red_nir_bands(info: dict) -> tuple[int, int]:
    """(banda rossa, banda infrarossa). Il COG SkyFi ha R, G, B e una quarta
    banda senza colorInterpretation che nei metadati dell'ordine è «nir»."""
    bands = info.get("bands") or []
    red = next((b["band"] for b in bands if _band_interp(b) == "red"), None)
    nir = next(
        (b["band"] for b in bands
         if _band_interp(b) in ("nir", "nearinfrared") or "nir" in (b.get("description") or "").lower()),
        None,
    )
    if nir is None and len(bands) == 4 and [_band_interp(b) for b in bands[:3]] == ["red", "green", "blue"] \
            and _band_interp(bands[3]) != "alpha":
        nir = 4
    if red is None or nir is None:
        raise OfflineMapError(
            "il GeoTIFF non ha la banda infrarossa: la mappa della vegetazione si fa dal COG (4 bande), non dal view-ready"
        )
    return red, nir


def _derived_vrt(width: int, height: int, info: dict, function: str, sources: list[tuple[str, int]]) -> str:
    root = ET.Element("VRTDataset", {"rasterXSize": str(width), "rasterYSize": str(height)})
    wkt = (info.get("coordinateSystem") or {}).get("wkt")
    if wkt:
        ET.SubElement(root, "SRS").text = wkt
    if info.get("geoTransform"):
        ET.SubElement(root, "GeoTransform").text = ", ".join(repr(float(v)) for v in info["geoTransform"])
    band = ET.SubElement(root, "VRTRasterBand", {"dataType": "Float32", "band": "1", "subClass": "VRTDerivedRasterBand"})
    ET.SubElement(band, "NoDataValue").text = "nan"
    ET.SubElement(band, "PixelFunctionType").text = function
    ET.SubElement(band, "SourceTransferType").text = "Float32"
    for path, index in sources:
        src = ET.SubElement(band, "SimpleSource")
        ET.SubElement(src, "SourceFilename", {"relativeToVRT": "0"}).text = path
        ET.SubElement(src, "SourceBand").text = str(index)
    return ET.tostring(root, encoding="unicode")


def ndvi_vrt(source: str, info: dict, work_dir: str) -> str:
    """VRT virtuale con NDVI = (NIR − R) / (NIR + R), calcolato da GDAL pixel
    per pixel (funzioni diff, sum, div): niente numpy, niente file intermedi.
    Fuori dall'area dell'ordine (NIR = R = 0) esce inf o NaN a seconda della
    versione di GDAL: la tavolozza li rende entrambi trasparenti."""
    red, nir = red_nir_bands(info)
    width, height = (info.get("size") or [0, 0])[:2]
    source = os.path.abspath(source) if not source.startswith("/vsi") else source
    paths = {}
    for name, function in (("diff", "diff"), ("sum", "sum")):
        paths[name] = os.path.join(work_dir, f"ndvi_{name}.vrt")
        with open(paths[name], "w", encoding="utf-8") as f:
            f.write(_derived_vrt(width, height, info, function, [(source, nir), (source, red)]))
    vrt = os.path.join(work_dir, "ndvi.vrt")
    with open(vrt, "w", encoding="utf-8") as f:
        f.write(_derived_vrt(width, height, info, "div", [(paths["diff"], 1), (paths["sum"], 1)]))
    return vrt


def ndvi_percentiles(vrt: str, work_dir: str, low: float = 2, high: float = 98) -> tuple[float, float]:
    """Percentili dell'NDVI della vegetazione (> 0,05) della scena, da una
    copia ridotta a 1/8 riscalata a Byte (-1..1 → 0..254, 255 = fuori area)."""
    small = os.path.join(work_dir, "ndvi_small.tif")
    run(["gdal_translate", "-q", "-ot", "Byte", "-scale", "-1", "1", "0", "254", "-a_nodata", "255",
         "-outsize", "12.5%", "12.5%", vrt, small])
    info = gdalinfo_hist(small)
    hist = (info.get("bands") or [{}])[0].get("histogram") or {}
    buckets = hist.get("buckets") or []
    lo, hi, n = float(hist.get("min", -0.5)), float(hist.get("max", 255.5)), len(buckets)
    values = []  # (valore NDVI al centro del bucket, conteggio)
    for i, count in enumerate(buckets):
        byte = lo + (i + 0.5) * (hi - lo) / n
        if byte >= 254.5:
            continue
        ndvi = byte / 254 * 2 - 1
        if ndvi > 0.05 and count:
            values.append((ndvi, count))
    total = sum(c for _, c in values)
    if not total:
        raise OfflineMapError("nella scena non c'è vegetazione misurabile (NDVI > 0,05)")

    def percentile(p):
        target, acc = total * p / 100, 0
        for value, count in values:
            acc += count
            if acc >= target:
                return value
        return values[-1][0]

    p_lo, p_hi = percentile(low), percentile(high)
    if p_hi - p_lo < 0.05:
        p_hi = p_lo + 0.05
    return p_lo, p_hi


def gdalinfo_hist(path: str) -> dict:
    text = run(["gdalinfo", "-json", "-hist", path])
    try:
        return json.loads(text[text.index("{"):])
    except ValueError as e:
        raise OfflineMapError(f"gdalinfo: risposta non leggibile ({e})") from e


def vegetation_stops(scale: str, p_lo: float | None = None, p_hi: float | None = None) -> list:
    if scale == "assoluta":
        return list(VEGETATION_STOPS)
    if scale != "relativa" or p_lo is None or p_hi is None:
        raise OfflineMapError(f"scala non valida: {scale}")
    fixed = [s for s in VEGETATION_STOPS if s[0] < VEG_FIRST_STOP]
    veg = [s for s in VEGETATION_STOPS if s[0] >= VEG_FIRST_STOP]
    a0, a1 = veg[0][0], veg[-1][0]
    # la parte relativa non deve scendere sotto il suolo nudo (0)
    start = max(p_lo, fixed[-1][0] + 0.01)
    stops = fixed + [(start + (x - a0) / (a1 - a0) * (p_hi - start), c) for x, c in veg]
    return stops


def color_table(stops: list) -> str:
    """Tavolozza per gdaldem color-relief -alpha: NaN (nv) e tutto ciò che sta
    sopra 1, cioè inf fuori area, trasparenti; l'NDVI vero sta in [-1, 1]."""
    lines = ["nv 0 0 0 0", f"-1 {' '.join(map(str, stops[0][1]))} 255"]
    lines += [f"{x:.4f} {r} {g} {b} 255" for x, (r, g, b) in stops if -1 < x < 1]
    lines += [f"1 {' '.join(map(str, stops[-1][1]))} 255", "1.0001 0 0 0 0"]
    return "\n".join(lines) + "\n"


def vegetation(
    source: str,
    out_tif: str,
    work_dir: str,
    on_step: Callable[[str, float], None] | None = None,
    scale: str = "relativa",
    max_zoom: int | None = None,
) -> dict:
    """COG a 4 bande → GeoTIFF COG a colori con lo stato della vegetazione,
    stessa griglia e risoluzione della mappa visibile. Ritorna il risultato
    di convert() più product, scale, ndvi_range, stops."""
    step = on_step or (lambda phase, fraction: None)
    step("analisi", 0)
    info = gdalinfo(source)
    vrt = ndvi_vrt(source, info, work_dir)
    p_lo = p_hi = None
    if scale == "relativa":
        p_lo, p_hi = ndvi_percentiles(vrt, work_dir)
    stops = vegetation_stops(scale, p_lo, p_hi)
    table = os.path.join(work_dir, "ndvi_colori.txt")
    with open(table, "w", encoding="utf-8") as f:
        f.write(color_table(stops))

    step("indice di vegetazione", 0)
    colored = os.path.join(work_dir, "vegetazione_rgba.tif")
    run(["gdaldem", "color-relief", vrt, table, colored, "-alpha",
         "-co", "TILED=YES", "-co", "COMPRESS=DEFLATE", "-co", "BIGTIFF=IF_SAFER"],
        lambda f: step("indice di vegetazione", f))
    try:
        result = convert(colored, out_tif, work_dir, on_step=step, max_zoom=max_zoom, tile_format="geotiff")
    finally:
        if os.path.exists(colored):
            os.remove(colored)
    result.update({
        "product": "vegetation",
        "scale": scale,
        "ndvi_range": [round(p_lo, 3), round(p_hi, 3)] if p_lo is not None else None,
        "stops": [[round(x, 4), list(c)] for x, c in stops],
    })
    return result


def vegetation_legend(path: str, stops: list, scale: str, title: str, subtitle: str = "") -> None:
    """Legenda PNG da mettere nel data package (ATAK non la disegna): barra
    dei colori con i valori NDVI e le classi a parole."""
    from PIL import Image, ImageDraw, ImageFont

    # Il font incorporato in Pillow non ha «ù» né le frecce: prima un font di
    # sistema, altrimenti quello incorporato con il testo senza accenti
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    ttf = next((c for c in candidates if os.path.exists(c)), None)

    def font(size):
        if ttf:
            return ImageFont.truetype(ttf, size)
        try:
            return ImageFont.load_default(size=size)
        except TypeError:  # Pillow < 10.1
            return ImageFont.load_default()

    def txt(s):
        if ttf:
            return s
        return s.replace("ù", "u'").replace("è", "e'").replace("à", "a'").replace("←", "<").replace("→", ">")

    title, subtitle = txt(title), txt(subtitle)

    stops = [(float(x), tuple(int(v) for v in c)) for x, c in stops]
    width, pad = 1100, 40
    img = Image.new("RGB", (width, 620), (250, 250, 246))
    d = ImageDraw.Draw(img)
    d.text((pad, 28), title, fill=(20, 24, 18), font=font(30))
    if subtitle:
        d.text((pad, 70), subtitle, fill=(90, 95, 85), font=font(20))
    lo, hi = stops[0][0], stops[-1][0]
    bar_y, bar_h, bar_w = 120, 50, width - 2 * pad
    xs = [s[0] for s in stops]
    for i in range(bar_w):
        v = lo + (hi - lo) * i / (bar_w - 1)
        j = max(k for k in range(len(xs)) if xs[k] <= v) if v >= xs[0] else 0
        if j >= len(xs) - 1:
            c = stops[-1][1]
        else:
            t = (v - xs[j]) / (xs[j + 1] - xs[j])
            c = tuple(round(stops[j][1][k] + t * (stops[j + 1][1][k] - stops[j][1][k])) for k in range(3))
        d.line([(pad + i, bar_y), (pad + i, bar_y + bar_h)], fill=c)
    for x, _ in stops[::2]:
        px = pad + (x - lo) / (hi - lo) * (bar_w - 1)
        d.line([(px, bar_y + bar_h), (px, bar_y + bar_h + 8)], fill=(60, 60, 60))
        d.text((px - 18, bar_y + bar_h + 12), f"{x:.2f}", fill=(60, 60, 60), font=font(16))
    d.text((pad, bar_y + bar_h + 40), txt("NDVI: meno vegetazione attiva ← → più vegetazione attiva"), fill=(60, 60, 60), font=font(18))
    y = bar_y + bar_h + 90
    for color, label in VEGETATION_LEGEND:
        d.rectangle([pad, y, pad + 36, y + 26], fill=color, outline=(80, 80, 80))
        d.text((pad + 52, y + 1), txt(label), fill=(20, 24, 18), font=font(22))
        y += 40
    note = ("Scala relativa: i colori coprono i valori presenti in questa area e in questa data."
            if scale == "relativa" else "Scala assoluta: soglie NDVI fisse, confrontabili fra aree diverse.")
    d.text((pad, y + 10), txt(note), fill=(90, 95, 85), font=font(18))
    d.text((pad, y + 40), txt("Misura la vegetazione vista dall'alto: non dice se sotto gli alberi si passa."),
           fill=(90, 95, 85), font=font(18))
    img.save(path)


def center_lat(info: dict) -> float:
    """Latitudine del centro di un raster in EPSG:3857 (per i metri a terra)."""
    center = (info.get("cornerCoordinates") or {}).get("center") or [0, 0]
    return math.degrees(math.atan(math.sinh(center[1] / 6378137)))


# ----------------------------------------------------------------------
# Data package
# ----------------------------------------------------------------------


def manifest_xml(package_name: str, package_uid: str, entry: str, extra_entries: list[str] = ()) -> bytes:
    root = ET.Element("MissionPackageManifest", {"version": "2"})
    config = ET.SubElement(root, "Configuration")
    ET.SubElement(config, "Parameter", {"name": "uid", "value": package_uid})
    ET.SubElement(config, "Parameter", {"name": "name", "value": package_name})
    contents = ET.SubElement(root, "Contents")
    for e in (entry, *extra_entries):
        ET.SubElement(contents, "Content", {"ignore": "false", "zipEntry": e})
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode").encode("utf-8")


def build_package(gpkg: str, zip_path: str, package_name: str, map_name: str, extension: str = ".gpkg",
                  extra_files: list[tuple[str, str]] = ()) -> tuple[str, int]:
    """Zip TAK con manifest + mappa (STORED: tile JPEG/PNG o GeoTIFF DEFLATE
    sono già compressi, ricomprimerli costa solo CPU) + eventuali file in più
    [(percorso, nome nel pacchetto)], es. la legenda della vegetazione.
    Ritorna (sha256, dimensione)."""
    package_uid = str(uuid.uuid4())
    entry = f"{package_uid}/{map_name}{extension}"
    extra = [(path, f"{package_uid}/{name}") for path, name in extra_files]
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        zf.writestr("MANIFEST/manifest.xml", manifest_xml(package_name, package_uid, entry, [e for _, e in extra]))
        zf.write(gpkg, entry, compress_type=zipfile.ZIP_STORED)
        for path, e in extra:
            zf.write(path, e)

    sha256 = hashlib.sha256()
    with open(zip_path, "rb") as f:
        while chunk := f.read(4 * 1024 * 1024):
            sha256.update(chunk)
    return sha256.hexdigest(), os.path.getsize(zip_path)
