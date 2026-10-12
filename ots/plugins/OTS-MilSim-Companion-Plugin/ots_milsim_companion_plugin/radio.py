# Portata radio LoRa per gli strumenti diagnostici del replay.
#
# Stima, non misura: serve a vedere sul campo dove due operatori con il tag
# sulla spalla avrebbero dovuto sentirsi, e dove il bosco ha mangiato il
# segnale. Tre pezzi, tutti modelli standard e dichiarati:
#
#   link budget  dai preset di Meshtastic (meshtastic.org/docs/overview/
#                radio-settings: 22 dBm, antenna 0 dBi), corretto per potenza,
#                guadagno d'antenna, perdita dovuta al corpo e margine;
#   propagazione modello a due raggi (terreno piatto): con antenne basse,
#                oltre il «punto di rottura» d_c = 4π·h1·h2/λ la perdita cresce
#                di 40 dB/decade invece che di 20. Prima di d_c, spazio libero.
#                La pianura del Po è piatta: niente modello del terreno;
#   vegetazione  modello di Weissberger sulla profondità di fogliame
#                attraversata lungo il raggio, pesata per la densità della cella
#                (copertura ESA WorldCover × NDVI Sentinel-2, vedi diag.py).
#
# Il player del browser rifà lo stesso calcolo per seguire gli operatori
# mentre si muovono (rpRadio* in index.html): le due versioni vanno tenute
# uguali, i test qui fissano i numeri.

import math

FREQ_MHZ = 869.525          # slot di default di LONG_FAST nella regione EU_868
SPEED_OF_LIGHT = 299_792_458.0
EARTH_RADIUS_M = 6_371_000.0

# Link budget in dB per preset (Meshtastic, 22 dBm e antenna 0 dBi). I Turbo
# (500 kHz) non sono ammessi in EU_868 e restano fuori.
PRESETS = {
    "SHORT_FAST": 143.0,
    "SHORT_SLOW": 145.5,
    "MEDIUM_FAST": 148.0,
    "MEDIUM_SLOW": 150.5,
    "LONG_FAST": 153.0,
    "LONG_MODERATE": 156.0,
}
PRESET_LABELS = {
    "SHORT_FAST": "Short Fast",
    "SHORT_SLOW": "Short Slow",
    "MEDIUM_FAST": "Medium Fast",
    "MEDIUM_SLOW": "Medium Slow",
    "LONG_FAST": "Long Fast",
    "LONG_MODERATE": "Long Moderate",
}
REFERENCE_TX_DBM = 22.0


def allowed_loss(preset: str, tx_dbm: float, antenna_dbi: float, body_loss_db: float,
                 margin_db: float, bodies: int = 2) -> float:
    """Perdita di percorso massima (dB) perché il pacchetto arrivi ancora.
    `bodies`: quante estremità hanno la radio addosso (2 fra due operatori,
    1 fra un operatore e il quartier generale)."""
    return (PRESETS[preset] + (tx_dbm - REFERENCE_TX_DBM) + 2 * antenna_dbi
            - bodies * body_loss_db - margin_db)


def breakpoint_m(h1: float, h2: float, freq_mhz: float = FREQ_MHZ) -> float:
    wavelength = SPEED_OF_LIGHT / (freq_mhz * 1e6)
    return 4 * math.pi * h1 * h2 / wavelength


def free_space_loss(d_m: float, freq_mhz: float = FREQ_MHZ) -> float:
    return 20 * math.log10(max(d_m, 1.0)) + 20 * math.log10(freq_mhz) - 27.55


def path_loss(d_m: float, h1: float, h2: float, freq_mhz: float = FREQ_MHZ) -> float:
    """Due raggi su terreno piatto: spazio libero fino a d_c, poi 40 dB/decade.
    Le due formule coincidono in d_c, quindi la curva è continua."""
    if d_m <= breakpoint_m(h1, h2, freq_mhz):
        return free_space_loss(d_m, freq_mhz)
    return 40 * math.log10(d_m) - 20 * math.log10(h1 * h2)


def vegetation_loss(depth_m: float, freq_mhz: float = FREQ_MHZ) -> float:
    """Weissberger: perdita dovuta a `depth_m` metri di fogliame attraversato.
    Lineare sotto i 14 m, poi crescita più lenta (il segnale aggira le
    chiome); la formula vale fino a ~400 m e oltre la si estende così com'è."""
    if depth_m <= 0:
        return 0.0
    f = (freq_mhz / 1000.0) ** 0.284
    if depth_m < 14:
        return 0.45 * f * depth_m
    return 1.33 * f * depth_m ** 0.588


def open_range_m(allowed_db: float, h1: float, h2: float, freq_mhz: float = FREQ_MHZ) -> float:
    """Distanza a cui la perdita in campo aperto raggiunge `allowed_db`."""
    dc = breakpoint_m(h1, h2, freq_mhz)
    if allowed_db <= free_space_loss(dc, freq_mhz):
        return 10 ** ((allowed_db - 20 * math.log10(freq_mhz) + 27.55) / 20)
    return 10 ** ((allowed_db + 20 * math.log10(h1 * h2)) / 40)


# ---------------------------------------------------------------------------
# Densità di fogliame di una cella (0 = niente ostacoli, 1 = bosco fitto)
# ---------------------------------------------------------------------------
#
# L'NDVI da solo dice quanto è verde, non quanto è alto: un prato ha NDVI
# alto e a 1,5 m non ferma niente, un bosco d'inverno ha NDVI basso e ferma
# lo stesso (tronchi e rami). Per questo si parte dalla copertura del suolo
# (ESA WorldCover) e l'NDVI del giorno dice solo quanto quella vegetazione
# era in foglia.

TREE, SHRUB, GRASS, CROP, BUILT, BARE, SNOW, WATER, WETLAND, MANGROVE, MOSS = (
    10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 100)


def _leaf(ndvi) -> float:
    """Quanto è in foglia (0,35 senza foglie … 1 pieno rigoglio); 0,8 se non si sa."""
    if ndvi is None:
        return 0.8
    return min(1.0, max(0.35, 0.35 + 0.65 * (ndvi - 0.2) / 0.5))


def foliage_density(cover: int, ndvi) -> float:
    if cover == TREE:
        return _leaf(ndvi)
    if cover in (SHRUB, MANGROVE):
        return 0.6 * _leaf(ndvi)
    if cover == WETLAND:
        return 0.3 * _leaf(ndvi)                # canneti
    if cover == CROP:
        # Solo le colture alte e rigogliose (mais d'estate) arrivano a 1,5 m
        if ndvi is None:
            return 0.0
        return 0.5 * min(1.0, max(0.0, (ndvi - 0.55) / 0.25))
    if cover == BUILT:
        return 1.0                              # muri: grossolano, ma ferma il segnale
    return 0.0


# ---------------------------------------------------------------------------
# Bolla: fin dove arriva il segnale, direzione per direzione
# ---------------------------------------------------------------------------

def destination(lat: float, lon: float, bearing_deg: float, d_m: float) -> tuple[float, float]:
    """Punto a `d_m` metri in direzione `bearing_deg` (approssimazione piana:
    esatta a sufficienza su qualche chilometro)."""
    b = math.radians(bearing_deg)
    dlat = d_m * math.cos(b) / EARTH_RADIUS_M
    dlon = d_m * math.sin(b) / (EARTH_RADIUS_M * math.cos(math.radians(lat)))
    return lat + math.degrees(dlat), lon + math.degrees(dlon)


def reach_m(allowed_db: float, h1: float, h2: float, density_along, step_m: float = 10.0,
            freq_mhz: float = FREQ_MHZ) -> float:
    """Distanza massima lungo un raggio. `density_along(d)` = densità di
    fogliame a `d` metri dall'origine (0-1). Si avanza a passi di `step_m`
    sommando il fogliame attraversato finché la perdita totale supera il
    budget; mai oltre la portata in campo aperto."""
    limit = open_range_m(allowed_db, h1, h2, freq_mhz)
    depth = 0.0
    d = 0.0
    while d + step_m <= limit:
        nxt = d + step_m
        depth += step_m * density_along(d + step_m / 2)
        if path_loss(nxt, h1, h2, freq_mhz) + vegetation_loss(depth, freq_mhz) > allowed_db:
            return d
        d = nxt
    return limit


def bubble(lat: float, lon: float, allowed_db: float, h1: float, h2: float, density_at,
           rays: int = 72, step_m: float = 10.0) -> list[tuple[float, float]]:
    """Contorno della bolla: `rays` direzioni equispaziate. `density_at(lat,
    lon)` → densità della cella (0-1; 0 fuori dalla mappa di copertura)."""
    points = []
    for k in range(rays):
        bearing = 360.0 * k / rays

        def along(d, bearing=bearing):
            return density_at(*destination(lat, lon, bearing, d))

        points.append(destination(lat, lon, bearing, reach_m(allowed_db, h1, h2, along, step_m)))
    return points
