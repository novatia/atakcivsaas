# Dati d'ambiente per gli strumenti diagnostici del replay.
#
# Per la zona di una giocata scarica, una volta sola (cache su disco):
#   - ESA WorldCover 2021 (10 m, CC BY 4.0): dove c'è bosco, cespuglio,
#     coltura, edificato — cioè cosa sta davvero fra due antenne a 1,5 m;
#   - Sentinel-2 L2A (10 m, Copernicus, dati aperti) della data più vicina
#     alla giocata: NDVI (quanto era in foglia) con le nuvole tolte grazie
#     alla classificazione della scena (SCL), più l'immagine a colori veri
#     per il layer del player.
# Le due fonti stanno su bucket pubblici AWS (nessun account): GDAL legge
# via /vsicurl solo la finestra che serve dai COG, non il file intero.
#
# Ne esce una griglia di densità di fogliame (0-255, radio.foliage_density)
# che il player usa per la bolla radio. Sentinel-3 non c'è di proposito:
# pixel da 300 m, su un campo da softair sarebbe un quadrato solo.

from __future__ import annotations

import array
import base64
import hashlib
import json
import math
import os
import shutil
import tempfile
import threading
import time
import traceback
from datetime import date, datetime, timedelta

from . import radio

STAC_SEARCH = "https://earth-search.aws.element84.com/v1/search"
WORLDCOVER_URL = ("https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
                  "ESA_WorldCover_10m_2021_v200_{tile}_Map.tif")
ATTRIBUTION = ("ESA WorldCover 2021 (CC BY 4.0) · Copernicus Sentinel-2 L2A, "
               "dati modificati, via Element 84 Earth Search")

CELL_M = 10.0                 # risoluzione nativa di entrambe le fonti
MAX_CELLS = 1000              # lato massimo della griglia (oltre, celle più grandi)
MARGIN_M = 4000.0             # attorno alle tracce: la bolla in Long Fast arriva a ~4 km
MAX_EXTENT_M = 30000.0        # oltre, la giocata è troppo sparsa per un'analisi utile
MAX_SCENE_AGE_DAYS = 20      # default, prima o dopo la giocata: oltre, la vegetazione può essere cambiata troppo
MAX_SCENE_CLOUD = 70.0        # copertura nuvolosa dichiarata della scena intera
MAX_SCENE_TRIES = 6           # scene lette al massimo cercando una limpida sulla zona
CLOUD_SCL = {3, 8, 9, 10}     # ombra, nuvole medie/alte, cirri
STALE_RUN_S = 20 * 60         # un «running» più vecchio è un processo morto: si riparte

_lock = threading.Lock()


class DiagError(Exception):
    pass


# ---------------------------------------------------------------------------
# Parti pure (testate)
# ---------------------------------------------------------------------------

def area_for(points: list[tuple[float, float]], margin_m: float = MARGIN_M) -> dict | None:
    """Riquadro attorno ai punti (lat, lon) col margine, arrotondato a ~100 m
    così giocate sullo stesso campo condividono la cache."""
    if not points:
        return None
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    mid = math.radians((min(lats) + max(lats)) / 2)
    dlat = margin_m / 111_320.0
    dlon = margin_m / (111_320.0 * math.cos(mid))
    w, e = min(lons) - dlon, max(lons) + dlon
    s, n = min(lats) - dlat, max(lats) + dlat
    width_m = (e - w) * 111_320.0 * math.cos(mid)
    height_m = (n - s) * 111_320.0
    if max(width_m, height_m) > MAX_EXTENT_M:
        return None
    return {k: round(v, 3) for k, v in (("w", w), ("s", s), ("e", e), ("n", n))}


def grid_size(area: dict) -> tuple[int, int]:
    mid = math.radians((area["s"] + area["n"]) / 2)
    width_m = (area["e"] - area["w"]) * 111_320.0 * math.cos(mid)
    height_m = (area["n"] - area["s"]) * 111_320.0
    cell = max(CELL_M, max(width_m, height_m) / MAX_CELLS)
    return max(1, round(width_m / cell)), max(1, round(height_m / cell))


def worldcover_tiles(area: dict) -> list[str]:
    """Tile WorldCover (3°×3°, nome dall'angolo sud-ovest) che coprono il riquadro."""
    tiles = []
    for lat0 in range(math.floor(area["s"] / 3) * 3, math.floor(area["n"] / 3) * 3 + 1, 3):
        for lon0 in range(math.floor(area["w"] / 3) * 3, math.floor(area["e"] / 3) * 3 + 1, 3):
            ns = f"{'N' if lat0 >= 0 else 'S'}{abs(lat0):02d}"
            ew = f"{'E' if lon0 >= 0 else 'W'}{abs(lon0):03d}"
            tiles.append(ns + ew)
    return tiles


def cache_key(area: dict, day: date, max_age_days: int = MAX_SCENE_AGE_DAYS) -> str:
    raw = json.dumps([area["w"], area["s"], area["e"], area["n"], day.isoformat(), max_age_days])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _inside(lon: float, lat: float, ring: list) -> bool:
    """Punto nel poligono (ray casting); ring = [[lon, lat], ...]."""
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def covers(feature: dict, area: dict) -> bool:
    """La zona sta tutta dentro l'impronta REALE della scena. Il bbox non
    basta: l'orbita taglia spesso la tile in diagonale e una parte resta
    vuota (verificato: tile 32TNQ del 4/10/2026, 36% senza dati, bbox pieno)."""
    geometry = feature.get("geometry") or {}
    polygons = {"Polygon": [geometry.get("coordinates")],
                "MultiPolygon": geometry.get("coordinates")}.get(geometry.get("type")) or []
    corners = [(area["w"], area["s"]), (area["w"], area["n"]), (area["e"], area["s"]), (area["e"], area["n"])]
    for polygon in polygons:
        if polygon and all(_inside(lon, lat, [list(p) for p in polygon[0]]) for lon, lat in corners):
            return True
    return False


def scene_day(feature: dict) -> date:
    return datetime.fromisoformat(feature["properties"]["datetime"].replace("Z", "+00:00")).date()


def rank_scenes(features: list[dict], area: dict, day: date, max_age_days: int = MAX_SCENE_AGE_DAYS) -> list[dict]:
    """Scene candidate in ordine di preferenza: quella del giorno della
    giocata, poi le più vicine prima o dopo, mai oltre `max_age_days` di
    distanza (la vegetazione cambia). A pari distanza la precedente, che
    mostra la vegetazione com'era prima del gioco. Solo scene che coprono
    tutta la zona e non sono quasi tutte nuvole; se sulla zona sono coperte
    lo decide build() leggendole, nell'ordine dato qui."""
    ranked = []
    for f in features:
        props = f.get("properties") or {}
        cloud = props.get("eo:cloud_cover")
        if cloud is None or not props.get("datetime") or cloud > MAX_SCENE_CLOUD or not covers(f, area):
            continue
        offset = (scene_day(f) - day).days          # negativo = prima della giocata
        if abs(offset) <= max_age_days:
            ranked.append(((abs(offset), offset > 0, cloud), f))
    return [f for _, f in sorted(ranked, key=lambda r: r[0])]


def pick_scene(features: list[dict], area: dict, day: date, max_age_days: int = MAX_SCENE_AGE_DAYS) -> dict | None:
    ranked = rank_scenes(features, area, day, max_age_days)
    return ranked[0] if ranked else None


def density_grid(cover: list[int], ndvi: list) -> bytes:
    """Densità di fogliame per cella, 0-255 (righe da nord a sud)."""
    return bytes(round(255 * radio.foliage_density(c, v)) for c, v in zip(cover, ndvi))


def ndvi_values(red: list[float], nir: list[float], scl: list[int]) -> list:
    """NDVI per cella; None dove manca il dato o c'è una nuvola."""
    out = []
    for r, n, s in zip(red, nir, scl):
        if s in CLOUD_SCL or s == 0 or (r + n) <= 0:
            out.append(None)
        else:
            out.append((n - r) / (n + r))
    return out


# ---------------------------------------------------------------------------
# Download (GDAL + STAC)
# ---------------------------------------------------------------------------

def _warp(sources: list[str], area: dict, size: tuple[int, int], out: str, ot: str, resampling: str) -> list:
    """Ritaglia/riproietta le sorgenti sulla griglia del riquadro (EPSG:4326)
    e ritorna i valori (righe da nord a sud)."""
    from . import offline_map

    cmd = [
        "gdalwarp", "-overwrite", "-q",
        "--config", "GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR",
        "--config", "CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif",
        "-t_srs", "EPSG:4326",
        "-te", str(area["w"]), str(area["s"]), str(area["e"]), str(area["n"]),
        "-ts", str(size[0]), str(size[1]),
        "-r", resampling, "-ot", ot, "-of", "ENVI",
        *[f"/vsicurl/{src}" for src in sources], out,
    ]
    try:
        offline_map.run(cmd, timeout=600)
    except offline_map.OfflineMapError as e:
        raise DiagError(f"lettura di {sources[0].rsplit('/', 1)[-1]} fallita: {e}") from e
    data = array.array({"Byte": "B", "Float32": "f", "UInt16": "H"}[ot])
    with open(out, "rb") as f:
        data.frombytes(f.read())
    if len(data) != size[0] * size[1]:
        raise DiagError(f"{os.path.basename(out)}: {len(data)} valori invece di {size[0] * size[1]}")
    return data.tolist()


def _png(source: str, area: dict, size: tuple[int, int], out_png: str, work: str) -> None:
    from . import offline_map

    tif = os.path.join(work, "visual.tif")
    try:
        offline_map.run([
            "gdalwarp", "-overwrite", "-q",
            "--config", "GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR",
            "-t_srs", "EPSG:4326",
            "-te", str(area["w"]), str(area["s"]), str(area["e"]), str(area["n"]),
            "-ts", str(size[0]), str(size[1]), "-r", "bilinear",
            f"/vsicurl/{source}", tif,
        ], timeout=600)
        offline_map.run(["gdal_translate", "-q", "-of", "PNG", tif, out_png], timeout=300)
    except offline_map.OfflineMapError as e:
        raise DiagError(f"immagine Sentinel-2 non generata: {e}") from e


def _search_scenes(area: dict, day: date, max_age_days: int) -> list[dict]:
    import requests

    params = {
        "collections": "sentinel-2-l2a",
        "bbox": f"{area['w']},{area['s']},{area['e']},{area['n']}",
        "datetime": f"{(day - timedelta(days=max_age_days)).isoformat()}T00:00:00Z/"
                    f"{(day + timedelta(days=max_age_days)).isoformat()}T23:59:59Z",
        "limit": 100,
    }
    r = requests.get(STAC_SEARCH, params=params, timeout=45)
    r.raise_for_status()
    return rank_scenes(r.json().get("features") or [], area, day, max_age_days)


def build(area: dict, day: date, folder: str, on_step=lambda text: None,
          max_age_days: int = MAX_SCENE_AGE_DAYS) -> dict:
    """Scarica e calcola tutto; scrive result.json (e s2.png) in `folder`."""
    from . import offline_map

    missing = offline_map.missing_tools()
    if "gdalwarp" in missing or "gdal_translate" in missing:
        raise DiagError("sul server mancano i comandi GDAL (gdalwarp, gdal_translate)")
    size = grid_size(area)
    work = tempfile.mkdtemp(prefix="milsim-diag-")
    notes = []
    try:
        on_step("copertura del suolo (ESA WorldCover)")
        cover = _warp([WORLDCOVER_URL.format(tile=t) for t in worldcover_tiles(area)],
                      area, size, os.path.join(work, "cover.bin"), "Byte", "near")

        on_step("ricerca della scena Sentinel-2")
        # Il giorno della giocata o, se manca, la scena più vicina prima o dopo
        # che sulla zona sia limpida (al massimo 30% fra nuvole e celle vuote):
        # la nuvolosità dichiarata è della tile intera (110 km), sul campo può
        # essere tutt'altra, quindi va letta. Se nessuna lo è, si tiene la meno
        # coperta fra quelle provate.
        scene, ndvi, cloudy = None, None, 1.0
        for candidate in _search_scenes(area, day, max_age_days)[:MAX_SCENE_TRIES]:
            assets = candidate["assets"]
            on_step(f"Sentinel-2 del {candidate['properties']['datetime'][:10]}: bande rossa e infrarossa")
            red = _warp([assets["red"]["href"]], area, size, os.path.join(work, "red.bin"), "Float32", "bilinear")
            nir = _warp([assets["nir"]["href"]], area, size, os.path.join(work, "nir.bin"), "Float32", "bilinear")
            scl = _warp([assets["scl"]["href"]], area, size, os.path.join(work, "scl.bin"), "Byte", "near")
            values = ndvi_values(red, nir, scl)
            share = sum(v is None for v in values) / len(values)
            if share < cloudy:
                scene, ndvi, cloudy = candidate, values, share
            if share <= 0.3:
                break
        s2 = None
        if scene:
            assets = scene["assets"]
            props = scene["properties"]
            on_step("immagine a colori veri")
            _png(assets["visual"]["href"], area, size, os.path.join(folder, "s2.png"), work)
            s2 = {
                "id": scene["id"],
                "date": props["datetime"][:10],
                "days_offset": (scene_day(scene) - day).days,   # negativo = prima della giocata
                "scene_cloud": round(props.get("eo:cloud_cover", 0), 1),
                "area_cloud": round(cloudy * 100, 1),
                "image": "s2.png",
            }
            if cloudy > 0.3:
                notes.append(f"il {round(cloudy * 100)}% della zona era coperto da nuvole: lì si "
                             "assume la vegetazione in foglia")
        else:
            ndvi = [None] * len(cover)
            notes.append(f"nessuna scena Sentinel-2 utilizzabile entro {max_age_days} giorni dalla "
                         "giocata: si assume la vegetazione in foglia")

        density = density_grid(cover, ndvi)
        result = {
            "area": area,
            "width": size[0],
            "height": size[1],
            "density": base64.b64encode(density).decode(),
            "sentinel2": s2,
            "notes": notes,
            "attribution": ATTRIBUTION,
            "built_at": datetime.utcnow().isoformat() + "Z",
        }
        with open(os.path.join(folder, "result.json"), "w") as f:
            json.dump(result, f)
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# Cache e stato del lavoro in background
# ---------------------------------------------------------------------------

def _folder(base: str, key: str) -> str:
    return os.path.join(base, "milsim_diag", key)


def _write_status(folder: str, **status) -> None:
    with open(os.path.join(folder, "status.json"), "w") as f:
        json.dump({**status, "at": time.time()}, f)


def environment(base: str, area: dict, day: date, max_age_days: int = MAX_SCENE_AGE_DAYS) -> dict:
    """Stato dei dati d'ambiente per riquadro e data; avvia lo scarico se
    serve. Stato su disco: con più processi OTS la richiesta successiva può
    arrivare a un altro, che deve vedere lo stesso lavoro."""
    key = cache_key(area, day, max_age_days)
    folder = _folder(base, key)
    result_path = os.path.join(folder, "result.json")
    if os.path.exists(result_path):
        with open(result_path) as f:
            return {"state": "ready", "key": key, **json.load(f)}

    with _lock:
        os.makedirs(folder, exist_ok=True)
        status_path = os.path.join(folder, "status.json")
        status = {}
        if os.path.exists(status_path):
            try:
                with open(status_path) as f:
                    status = json.load(f)
            except (OSError, ValueError):
                status = {}
        fresh = time.time() - status.get("at", 0) < STALE_RUN_S
        if status.get("state") == "running" and fresh:
            return {"state": "running", "key": key, "step": status.get("step")}
        if status.get("state") == "error" and fresh and status.get("retry_after", 0) > time.time():
            return {"state": "error", "key": key, "error": status.get("error")}
        _write_status(folder, state="running", step="avvio")

    def work():
        try:
            build(area, day, folder, on_step=lambda text: _write_status(folder, state="running", step=text),
                  max_age_days=max_age_days)
            _write_status(folder, state="ready")
        except BaseException as e:
            message = str(e) if isinstance(e, DiagError) else f"{type(e).__name__}: {e}"
            _write_status(folder, state="error", error=message, retry_after=time.time() + 120,
                          trace=traceback.format_exc()[-2000:])

    threading.Thread(target=work, daemon=True, name=f"milsim-diag-{key}").start()
    return {"state": "running", "key": key, "step": "avvio"}


def image_path(base: str, key: str) -> str | None:
    if not key.isalnum():
        return None
    path = os.path.join(_folder(base, key), "s2.png")
    return path if os.path.exists(path) else None
