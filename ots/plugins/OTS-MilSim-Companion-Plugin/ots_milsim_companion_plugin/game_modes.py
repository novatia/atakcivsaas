# Anagrafica delle modalità di gioco.
#
# Ogni modalità dichiara quali tipi di marker e di area servono (con minimo e
# massimo); i tipi definiscono colore/forma nella UI dell'editor e il CoT con
# cui vengono pushati agli EUD al Play. La resa su ATAK/WinTAK sfrutta la
# simbologia nativa (affiliazione) dove i colori coincidono con quelli chiesti:
# rombo rosso = ostile, quadrato blu = amico, giallo = sconosciuto; per il
# viola dei bomb site (colore non previsto dalla simbologia) si usano gli
# "spot marker" colorati.


def argb(hex_argb: str) -> int:
    """'FFE53935' -> intero ARGB con segno, il formato colore di CoT/ATAK."""
    value = int(hex_argb, 16)
    return value - 0x100000000 if value > 0x7FFFFFFF else value


MARKER_TYPES = {
    "spawn_a": {
        "label": "Spawn Team A",
        "color": "#E53935",           # rosso
        "shape": "diamond",
        "cot_type": "a-h-G",          # affiliazione ostile: su ATAK rombo rosso
        "argb": argb("FFE53935"),
        "callsign": "SPAWN A",
        "spot": False,
    },
    "spawn_b": {
        "label": "Spawn Team B",
        "color": "#1E88E5",           # blu
        "shape": "square",
        "cot_type": "a-f-G",          # affiliazione amica: su ATAK quadrato/rettangolo blu
        "argb": argb("FF1E88E5"),
        "callsign": "SPAWN B",
        "spot": False,
    },
    "flag": {
        "label": "Bandiera",
        "color": "#FFD800",           # giallo
        "shape": "circle",
        "cot_type": "a-u-G",          # affiliazione sconosciuta: su ATAK simbolo giallo
        "argb": argb("FFFFD800"),
        "callsign": "BANDIERA",
        "spot": False,
    },
    "bomb_site_a": {
        "label": "Bomb site A",
        "color": "#9C27B0",           # viola
        "shape": "square",
        "cot_type": "b-m-p-s-m",      # spot marker: pallino colorato (il viola non esiste come affiliazione)
        "argb": argb("FF9C27B0"),
        "callsign": "BOMB A",
        "spot": True,
    },
    "bomb_site_b": {
        "label": "Bomb site B",
        "color": "#9C27B0",
        "shape": "square",
        "cot_type": "b-m-p-s-m",
        "argb": argb("FF9C27B0"),
        "callsign": "BOMB B",
        "spot": True,
    },
    "dom_point": {
        "label": "Punto di dominio",
        "color": "#FB8C00",           # arancione (i colori affiliazione sono già presi)
        "shape": "circle",
        "cot_type": "b-m-p-s-m",      # spot marker colorato, come i bomb site
        "argb": argb("FFFB8C00"),
        "callsign": "DOM",
        "spot": True,
    },
}

ZONE_TYPES = {
    "bomb_area": {
        "label": "Area valida ordigni",
        "color": "#9C27B0",
        "stroke_argb": argb("FF9C27B0"),
        "fill_argb": argb("409C27B0"),   # stesso colore, alpha 25%
    },
    "dom_area": {
        "label": "Area punto di dominio",
        "color": "#FB8C00",
        "stroke_argb": argb("FFFB8C00"),
        "fill_argb": argb("40FB8C00"),
    },
    "field_boundary": {
        "label": "Perimetro campo",
        "color": "#43A047",
        "stroke_argb": argb("FF43A047"),
        "fill_argb": argb("2043A047"),
    },
}

GAME_MODES = {
    "ctf": {
        "name": "Capture the Flag",
        "icon": "🚩",
        "description": "Due squadre, una o due bandiere da catturare e riportare al proprio spawn.",
        "markers": {
            "spawn_a": {"min": 1, "max": 4},
            "spawn_b": {"min": 1, "max": 4},
            "flag": {"min": 1, "max": 2},
        },
        "zones": {
            "field_boundary": {"min": 0, "max": 1},
        },
    },
    "bomb": {
        "name": "Bomb Defusal",
        "icon": "💣",
        "description": "Una squadra pianta l'ordigno nel sito A o B dentro l'area valida, l'altra difende e disinnesca.",
        "markers": {
            "spawn_a": {"min": 1, "max": 4},
            "spawn_b": {"min": 1, "max": 4},
            "bomb_site_a": {"min": 1, "max": 1},
            "bomb_site_b": {"min": 1, "max": 1},
        },
        "zones": {
            "bomb_area": {"min": 1, "max": 1},
            "field_boundary": {"min": 0, "max": 1},
        },
    },
    "tdm": {
        "name": "Team Deathmatch",
        "icon": "⚔️",
        "description": "Due squadre, si vince a eliminazioni: servono solo gli spawn point.",
        "markers": {
            "spawn_a": {"min": 1, "max": 4},
            "spawn_b": {"min": 1, "max": 4},
        },
        "zones": {
            "field_boundary": {"min": 0, "max": 1},
        },
    },
    "dom": {
        "name": "Dominio",
        "icon": "🏰",
        "description": "Due squadre si contendono N punti di dominio (minimo 2), ognuno con la propria area di validità: il punto è preso quando la squadra lo controlla (meccanica di cattura in arrivo).",
        "markers": {
            "spawn_a": {"min": 1, "max": 4},
            "spawn_b": {"min": 1, "max": 4},
            "dom_point": {"min": 2, "max": 8},
        },
        "zones": {
            "dom_area": {"min": 2, "max": 8},
            "field_boundary": {"min": 0, "max": 1},
        },
        # Al Play il numero di aree deve corrispondere al numero di punti
        "paired": {"dom_point": "dom_area"},
    },
}


def serialize_registry() -> dict:
    return {
        "marker_types": MARKER_TYPES,
        "zone_types": ZONE_TYPES,
        "modes": GAME_MODES,
    }


def validate_template(mode_key: str, markers: list, zones: list, for_play: bool = False) -> list[str]:
    """Errori di coerenza tra template e modalità.

    Al salvataggio (for_play=False) si controllano solo tipi ammessi e massimi,
    così un template si può salvare a metà; al Play (for_play=True) si
    verificano anche i minimi.
    """
    errors = []
    mode = GAME_MODES.get(mode_key)
    if not mode:
        return [f"Modalità sconosciuta: {mode_key}"]

    marker_counts: dict[str, int] = {}
    for marker in markers:
        mtype = marker.get("type")
        if mtype not in MARKER_TYPES:
            errors.append(f"Tipo di marker sconosciuto: {mtype}")
            continue
        if mtype not in mode["markers"]:
            errors.append(f"Il marker '{MARKER_TYPES[mtype]['label']}' non è previsto in {mode['name']}")
            continue
        if not isinstance(marker.get("lat"), (int, float)) or not isinstance(marker.get("lon"), (int, float)):
            errors.append(f"Coordinate mancanti per un marker '{MARKER_TYPES[mtype]['label']}'")
        marker_counts[mtype] = marker_counts.get(mtype, 0) + 1

    zone_counts: dict[str, int] = {}
    for zone in zones:
        ztype = zone.get("type")
        if ztype not in ZONE_TYPES:
            errors.append(f"Tipo di area sconosciuto: {ztype}")
            continue
        if ztype not in mode["zones"]:
            errors.append(f"L'area '{ZONE_TYPES[ztype]['label']}' non è prevista in {mode['name']}")
            continue
        points = zone.get("points") or []
        if len(points) < 3:
            errors.append(f"L'area '{ZONE_TYPES[ztype]['label']}' ha meno di 3 vertici")
        zone_counts[ztype] = zone_counts.get(ztype, 0) + 1

    for mtype, limits in mode["markers"].items():
        count = marker_counts.get(mtype, 0)
        if count > limits["max"]:
            errors.append(f"Troppi marker '{MARKER_TYPES[mtype]['label']}': {count} (max {limits['max']})")
        if for_play and count < limits["min"]:
            errors.append(f"Manca il marker '{MARKER_TYPES[mtype]['label']}' (minimo {limits['min']})")

    for ztype, limits in mode["zones"].items():
        count = zone_counts.get(ztype, 0)
        if count > limits["max"]:
            errors.append(f"Troppe aree '{ZONE_TYPES[ztype]['label']}': {count} (max {limits['max']})")
        if for_play and count < limits["min"]:
            errors.append(f"Manca l'area '{ZONE_TYPES[ztype]['label']}' (minimo {limits['min']})")

    # Vincoli di accoppiamento (es. Dominio: un'area di validità per ogni punto)
    if for_play:
        for mtype, ztype in (mode.get("paired") or {}).items():
            m_count = marker_counts.get(mtype, 0)
            z_count = zone_counts.get(ztype, 0)
            if m_count != z_count:
                errors.append(
                    f"Servono tante aree '{ZONE_TYPES[ztype]['label']}' quanti marker "
                    f"'{MARKER_TYPES[mtype]['label']}': ora {z_count} aree per {m_count} marker"
                )

    return errors
