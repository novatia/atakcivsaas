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
import sys
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
# Copernicus DEM GLO-30: quote sul geoide EGM2008 (cioè sul livello del mare),
# tile da 1°×1°, pubblico su AWS. È un modello di SUPERFICIE: nei boschi e
# sugli edifici può misurare la cima, vedi remove_canopy().
DEM_URL = ("https://copernicus-dem-30m.s3.amazonaws.com/Copernicus_DSM_COG_10_{tile}_DEM/"
           "Copernicus_DSM_COG_10_{tile}_DEM.tif")
DEM_ATTRIBUTION = ("Copernicus DEM GLO-30 © DLR e.V. 2010-2014 e © Airbus Defence and Space GmbH "
                   "2014-2018, fornito nell'ambito di COPERNICUS da Unione Europea ed ESA · ESA WorldCover 2021")
DEM_CELL_DEG = 1 / 3600       # 1 secondo d'arco, la risoluzione nativa
TERRAIN_MARGIN_M = 500.0      # attorno alle tracce: serve solo dove si è camminato
MAX_TERRAIN_CELLS = 1500

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
    """Errore leggibile; `detail` = comando eseguito e output completo, per i
    log della tab Manutenzione."""

    def __init__(self, message: str, detail: str | None = None):
        super().__init__(message)
        self.detail = detail


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


def dem_tiles(area: dict) -> list[str]:
    """Tile Copernicus DEM (1°×1°, nome dall'angolo sud-ovest) che coprono il riquadro."""
    tiles = []
    for lat0 in range(math.floor(area["s"]), math.floor(area["n"]) + 1):
        for lon0 in range(math.floor(area["w"]), math.floor(area["e"]) + 1):
            tiles.append(f"{'N' if lat0 >= 0 else 'S'}{abs(lat0):02d}_00_"
                         f"{'E' if lon0 >= 0 else 'W'}{abs(lon0):03d}_00")
    return tiles


def terrain_size(area: dict) -> tuple[int, int]:
    """Griglia a 1 secondo d'arco, ridotta se la zona è grande."""
    w = (area["e"] - area["w"]) / DEM_CELL_DEG
    h = (area["n"] - area["s"]) / DEM_CELL_DEG
    scale = max(1.0, max(w, h) / MAX_TERRAIN_CELLS)
    return max(1, round(w / scale)), max(1, round(h / scale))


OBSTACLES = {radio.TREE, radio.BUILT}
MIN_CANOPY_M = 3.0            # sotto, il gradino al margine è rumore del DEM: niente correzione
MAX_CANOPY_M = 35.0


def _neighbours(i: int, width: int, height: int):
    r, c = divmod(i, width)
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if (dr or dc) and 0 <= r + dr < height and 0 <= c + dc < width:
                yield (r + dr) * width + (c + dc)


def canopy_offset(elevation: list[float], cover: list[int], width: int, height: int, kind: int = radio.TREE):
    """Quanto la superficie del DEM sta sopra il terreno in quel tipo di
    copertura, misurato dove si vede: ai margini, confrontando ogni cella
    coperta con le celle libere accanto (mediana, robusta ai pendii).
    None se i margini sono troppo pochi per dirlo."""
    steps = []
    for i, c in enumerate(cover):
        if c != kind:
            continue
        free = [elevation[j] for j in _neighbours(i, width, height) if cover[j] not in OBSTACLES]
        if free:
            steps.append(elevation[i] - sum(free) / len(free))
    if len(steps) < 10:
        return None
    import statistics

    return statistics.median(steps)


def remove_canopy(elevation: list[float], cover: list[int], width: int, height: int) -> tuple[list[float], dict]:
    """Toglie alberi ed edifici dal modello di superficie, se ci sono.

    Copernicus DEM è un modello di superficie, ma quanto «vede» le chiome
    dipende dal bosco: misurato sul campo della giocata del 2026-10-11
    (colline dell'Oltrepò, latifoglie) il gradino al margine del bosco è di
    0-1 m, cioè il DEM segue già il terreno. Si misura quindi zona per zona
    e si sottrae l'altezza stimata, uniforme, solo se supera MIN_CANOPY_M:
    in un bosco continuo la forma del pendio resta quella del DEM e sparisce
    il gradino che il dislivello conterebbe come salita. (Ricostruire il
    terreno dalle radure è stato provato e scartato: con l'84% di bosco
    inventava pendii di 30 m.)"""
    out = list(elevation)
    applied = {}
    for kind in (radio.TREE, radio.BUILT):
        offset = canopy_offset(elevation, cover, width, height, kind)
        if offset is not None and offset >= MIN_CANOPY_M:
            offset = min(offset, MAX_CANOPY_M)
            for i, c in enumerate(cover):
                if c == kind:
                    out[i] -= offset
            applied[kind] = round(offset, 1)
        else:
            applied[kind] = 0.0 if offset is not None else None
    return out, {"tree_m": applied[radio.TREE], "built_m": applied[radio.BUILT]}


def encode_elevation(values: list[float]) -> str:
    """Quote in decimetri, int16 little-endian, base64 (±3276 m bastano)."""
    data = array.array("h", (max(-32768, min(32767, round(v * 10))) for v in values))
    if data.itemsize != 2:
        raise DiagError("int16 non disponibile")
    if sys.byteorder != "little":
        data.byteswap()
    return base64.b64encode(data.tobytes()).decode()


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
        "gdalwarp", "-overwrite",
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
        raise DiagError(f"lettura di {sources[0].rsplit('/', 1)[-1]} fallita: {e}",
                        detail=" ".join(cmd) + "\n\n" + str(e) + _probe(cmd, sources[0])) from e
    data = array.array({"Byte": "B", "Float32": "f", "UInt16": "H"}[ot])
    with open(out, "rb") as f:
        data.frombytes(f.read())
    if len(data) != size[0] * size[1]:
        raise DiagError(f"{os.path.basename(out)}: {len(data)} valori invece di {size[0] * size[1]}")
    return data.tolist()


def _probe(cmd: list[str], url: str) -> str:
    """Dopo un fallimento muto di gdalwarp (visto sul server il 2026-10-12:
    codice 1 e nessun output, mentre da shell gdalinfo sullo stesso URL
    funziona): rilancia lo stesso comando catturando stdout e stderr a parte
    (senza passare dal lettore non bloccante di offline_map.run), mostra le
    variabili d'ambiente di GDAL/PROJ del processo OTS e prova l'URL con
    gdalinfo e con una GET diretta. Dicono se è l'ambiente, PROJ, la rete o GDAL."""
    import subprocess

    parts = []
    env_keys = sorted(k for k in os.environ if k.startswith(("GDAL", "PROJ", "CPL", "CURL", "SSL", "HTTP", "LD_")))
    parts.append("ambiente del processo OTS:\n" + ("\n".join(f"  {k}={os.environ[k]}" for k in env_keys) or "  (nessuna variabile GDAL/PROJ/CURL)")
                 + f"\n  PATH={os.environ.get('PATH', '')}")

    def capture(title, command):
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=180)
            parts.append(f"{title}: codice {done.returncode}\n[stdout]\n{done.stdout[-1500:]}\n[stderr]\n{done.stderr[-2500:]}")
        except BaseException as e:
            parts.append(f"{title}: {type(e).__name__}: {e}")

    retry = [c for c in cmd if c != "-q"] + ["--debug", "on"]
    capture("stesso gdalwarp rilanciato con debug", retry)
    capture("gdalinfo sullo stesso URL", ["gdalinfo", f"/vsicurl/{url}"])
    capture("projinfo EPSG:4326", ["projinfo", "EPSG:4326"])
    try:
        import requests

        r = requests.get(url, headers={"Range": "bytes=0-1023"}, timeout=20, stream=True)
        r.close()
        parts.append(f"GET diretto da Python: HTTP {r.status_code}")
    except BaseException as e:
        parts.append(f"GET diretto da Python: {type(e).__name__}: {e}")
    return "\n\n--- verifica automatica ---\n" + "\n\n".join(parts)


def _png(source: str, area: dict, size: tuple[int, int], out_png: str, work: str) -> None:
    from . import offline_map

    tif = os.path.join(work, "visual.tif")
    cmd = [
        "gdalwarp", "-overwrite",
        "--config", "GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR",
        "-t_srs", "EPSG:4326",
        "-te", str(area["w"]), str(area["s"]), str(area["e"]), str(area["n"]),
        "-ts", str(size[0]), str(size[1]), "-r", "bilinear",
        f"/vsicurl/{source}", tif,
    ]
    try:
        offline_map.run(cmd, timeout=600)
        cmd = ["gdal_translate", "-q", "-of", "PNG", tif, out_png]
        offline_map.run(cmd, timeout=300)
    except offline_map.OfflineMapError as e:
        raise DiagError(f"immagine Sentinel-2 non generata: {e}", detail=" ".join(cmd) + "\n\n" + str(e)) from e


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


def build_terrain(area: dict, folder: str, on_step=lambda text: None) -> dict:
    """Quote del terreno (DEM corretto per boschi ed edifici); scrive result.json."""
    from . import offline_map

    missing = offline_map.missing_tools()
    if "gdalwarp" in missing:
        raise DiagError("sul server manca il comando GDAL gdalwarp")
    size = terrain_size(area)
    work = tempfile.mkdtemp(prefix="milsim-terrain-")
    try:
        on_step("modello del terreno (Copernicus DEM)")
        elevation = _warp([DEM_URL.format(tile=t) for t in dem_tiles(area)],
                          area, size, os.path.join(work, "dem.bin"), "Float32", "bilinear")
        on_step("boschi ed edifici (ESA WorldCover)")
        cover = _warp([WORLDCOVER_URL.format(tile=t) for t in worldcover_tiles(area)],
                      area, size, os.path.join(work, "cover.bin"), "Byte", "mode")
        on_step("altezza di chiome ed edifici")
        ground, canopy = remove_canopy(elevation, cover, *size)
        covered = sum(c in OBSTACLES for c in cover) / len(cover)
        result = {
            "area": area,
            "width": size[0],
            "height": size[1],
            "elevation_dm": encode_elevation(ground),
            "obstacle_share": round(covered * 100, 1),
            "canopy_removed": canopy,
            "attribution": DEM_ATTRIBUTION,
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
    """Stato dei dati d'ambiente per riquadro e data; avvia lo scarico se serve."""
    key = cache_key(area, day, max_age_days)
    return _job(base, key, lambda folder, on_step: build(area, day, folder, on_step, max_age_days=max_age_days))


def terrain(base: str, area: dict) -> dict:
    """Stato del modello del terreno per il riquadro; avvia lo scarico se serve."""
    key = "t" + hashlib.sha1(json.dumps([area["w"], area["s"], area["e"], area["n"]]).encode()).hexdigest()[:15]
    return _job(base, key, lambda folder, on_step: build_terrain(area, folder, on_step))


def _job(base: str, key: str, builder) -> dict:
    """Risultato in cache, o lavoro in corso, o avvio del lavoro. Stato su
    disco: con più processi OTS la richiesta successiva può arrivare a un
    altro, che deve vedere lo stesso lavoro."""
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
            builder(folder, lambda text: _write_status(folder, state="running", step=text))
            _write_status(folder, state="ready")
        except BaseException as e:
            message = str(e) if isinstance(e, DiagError) else f"{type(e).__name__}: {e}"
            _write_status(folder, state="error", error=message, retry_after=time.time() + 120,
                          detail=getattr(e, "detail", None), trace=traceback.format_exc()[-4000:])

    threading.Thread(target=work, daemon=True, name=f"milsim-diag-{key}").start()
    return {"state": "running", "key": key, "step": "avvio"}


# ---------------------------------------------------------------------------
# Log e autotest (tab Manutenzione)
# ---------------------------------------------------------------------------

def list_jobs(base: str) -> list[dict]:
    """Scarichi in cache o tentati: stato, ultimo passo, errore completo."""
    root = os.path.join(base, "milsim_diag")
    jobs = []
    if not os.path.isdir(root):
        return jobs
    for key in sorted(os.listdir(root)):
        folder = os.path.join(root, key)
        if not os.path.isdir(folder) or not key.isalnum():
            continue
        status = {}
        try:
            with open(os.path.join(folder, "status.json")) as f:
                status = json.load(f)
        except (OSError, ValueError):
            pass
        info = {}
        result = os.path.join(folder, "result.json")
        if os.path.exists(result):
            try:
                with open(result) as f:
                    data = json.load(f)
                info = {k: data.get(k) for k in ("area", "width", "height", "sentinel2", "notes",
                                                 "canopy_removed", "obstacle_share", "built_at")}
            except (OSError, ValueError):
                pass
        jobs.append({
            "key": key,
            "kind": "terreno" if key.startswith("t") else "ambiente",
            "state": "ready" if os.path.exists(result) else status.get("state", "?"),
            "step": status.get("step"),
            "error": status.get("error"),
            "detail": status.get("detail"),
            "trace": status.get("trace"),
            "at": status.get("at"),
            **info,
        })
    return sorted(jobs, key=lambda j: j.get("at") or 0, reverse=True)


def retry(base: str, key: str) -> bool:
    """Dimentica un tentativo fallito o un risultato: la prossima richiesta riparte."""
    if not key.isalnum():
        return False
    folder = _folder(base, key)
    if not os.path.isdir(folder):
        return False
    shutil.rmtree(folder, ignore_errors=True)
    return True


SELFTEST_HOSTS = (
    ("Catalogo Sentinel-2 (Element 84)", STAC_SEARCH + "?collections=sentinel-2-l2a&limit=1"),
    ("Sentinel-2 COG (AWS)", "https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs/"
                             "32/T/NQ/2026/10/S2A_32TNQ_20261009_1_L2A/B04.tif"),
    ("ESA WorldCover (AWS)", WORLDCOVER_URL.format(tile="N45E009")),
    ("Copernicus DEM (AWS)", DEM_URL.format(tile="N44_00_E009_00")),
)


def selftest() -> list[dict]:
    """Prove dal server, con l'output completo: comandi GDAL, rete, una
    lettura vera di una finestra del DEM via /vsicurl come fa build_terrain."""
    import requests

    from . import offline_map

    checks = []

    def check(name, fn):
        try:
            ok, output = fn()
        except BaseException as e:
            ok, output = False, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}"
        checks.append({"name": name, "ok": ok, "output": output})

    def tools():
        missing = offline_map.missing_tools()
        found = {t: shutil.which(t) for t in offline_map.GDAL_TOOLS}
        return not missing, "\n".join(f"{t}: {p or 'NON TROVATO'}" for t, p in found.items())

    def version():
        return True, offline_map.run(["gdalwarp", "--version"], timeout=30).strip()

    def drivers():
        out = offline_map.run(["gdalinfo", "--formats"], timeout=30)
        wanted = [line.strip() for line in out.splitlines() if line.strip().split(" ")[0] in ("ENVI", "GTiff", "PNG")]
        return len(wanted) == 3, "\n".join(wanted) or out[-800:]

    check("Comandi GDAL", tools)
    check("Versione GDAL", version)
    check("Formati ENVI, GTiff, PNG", drivers)
    for name, url in SELFTEST_HOSTS:
        def reach(url=url):
            r = requests.get(url, headers={"Range": "bytes=0-1023"}, timeout=20, stream=True)
            r.close()
            return r.status_code < 400, f"GET {url}\nHTTP {r.status_code}"
        check(f"Rete: {name}", reach)

    def dem_window():
        work = tempfile.mkdtemp(prefix="milsim-selftest-")
        try:
            area = {"w": 9.540, "s": 44.925, "e": 9.560, "n": 44.940}
            values = _warp([DEM_URL.format(tile="N44_00_E009_00")], area, (20, 20),
                           os.path.join(work, "dem.bin"), "Float32", "bilinear")
            return True, f"20×20 quote lette: min {min(values):.1f} m, max {max(values):.1f} m"
        except DiagError as e:
            return False, f"{e}\n\n{e.detail or ''}"
        finally:
            shutil.rmtree(work, ignore_errors=True)

    check("Lettura via /vsicurl di una finestra del DEM", dem_window)
    return checks


def image_path(base: str, key: str) -> str | None:
    if not key.isalnum():
        return None
    path = os.path.join(_folder(base, key), "s2.png")
    return path if os.path.exists(path) else None
