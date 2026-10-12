"""Dati d'ambiente degli strumenti diagnostici: parti senza rete né GDAL."""

from datetime import date

from ots_milsim_companion_plugin import diag, radio

CODOGNO = [(45.160, 9.700), (45.166, 9.712)]


def test_riquadro_con_margine_e_griglia_da_10_m():
    area = diag.area_for(CODOGNO)
    assert area["s"] < 45.160 - 0.03 and area["n"] > 45.166 + 0.03     # ~4 km = 0,036°
    w, h = diag.grid_size(area)
    assert 800 < w <= diag.MAX_CELLS and 800 < h <= diag.MAX_CELLS


def test_giocata_troppo_sparsa():
    assert diag.area_for([(45.0, 9.0), (45.5, 9.5)]) is None
    assert diag.area_for([]) is None


def test_tile_worldcover():
    assert diag.worldcover_tiles({"w": 9.6, "s": 45.1, "e": 9.8, "n": 45.2}) == ["N45E009"]
    # A cavallo di due tile in longitudine
    assert diag.worldcover_tiles({"w": 11.9, "s": 45.1, "e": 12.1, "n": 45.2}) == ["N45E009", "N45E012"]


def test_cache_condivisa_fra_giocate_sullo_stesso_campo():
    area = diag.area_for(CODOGNO)
    assert diag.cache_key(area, date(2026, 10, 11)) == diag.cache_key(dict(area), date(2026, 10, 11))
    assert diag.cache_key(area, date(2026, 10, 11)) != diag.cache_key(area, date(2026, 10, 12))
    assert diag.cache_key(area, date(2026, 10, 11)) != diag.cache_key(area, date(2026, 10, 11), max_age_days=30)


def _scene(id_, day, cloud, bbox=(9.0, 45.0, 10.5, 46.0), ring=None):
    w, so, e, n = bbox
    ring = ring or [[w, so], [e, so], [e, n], [w, n], [w, so]]
    return {"id": id_, "bbox": list(bbox), "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": {"datetime": f"{day}T10:28:40Z", "eo:cloud_cover": cloud}}


def test_scelta_della_scena():
    area = {"w": 9.6, "s": 45.1, "e": 9.8, "n": 45.2}
    day = date(2026, 10, 11)
    scenes = [
        _scene("dopo-la-giocata", "2026-10-12", 0),
        _scene("4-giorni-prima", "2026-10-07", 2),
        _scene("stesso-giorno", "2026-10-11", 60),
        _scene("non-copre-il-campo", "2026-10-11", 0, bbox=(9.7, 45.0, 10.5, 46.0)),
        _scene("troppo-nuvolosa", "2026-10-10", 95),
        _scene("troppo-vecchia", "2026-09-01", 0),
    ]
    # Prima il giorno della giocata, poi le più vicine prima o dopo; mai oltre il limite
    assert [f["id"] for f in diag.rank_scenes(scenes, area, day)] == ["stesso-giorno", "dopo-la-giocata", "4-giorni-prima"]
    assert [f["id"] for f in diag.rank_scenes(scenes, area, day, max_age_days=60)][-1] == "troppo-vecchia"
    assert diag.pick_scene([scenes[3], scenes[4], scenes[5]], area, day) is None
    # A pari distanza vince quella precedente
    tie = [_scene("un-giorno-dopo", "2026-10-12", 0), _scene("un-giorno-prima", "2026-10-10", 30)]
    assert diag.pick_scene(tie, area, day)["id"] == "un-giorno-prima"


def test_ndvi_senza_nuvole_e_densita():
    red = [0.05, 0.05, 0.10, 0.0]
    nir = [0.45, 0.45, 0.10, 0.0]
    scl = [4, 9, 5, 0]            # vegetazione, nuvola, suolo nudo, nessun dato
    ndvi = diag.ndvi_values(red, nir, scl)
    assert round(ndvi[0], 2) == 0.8 and ndvi[1] is None and ndvi[2] == 0 and ndvi[3] is None
    cover = [radio.TREE, radio.TREE, radio.GRASS, radio.BUILT]
    assert list(diag.density_grid(cover, ndvi)) == [255, round(255 * 0.8), 0, 255]


def test_impronta_tagliata_in_diagonale():
    # Bbox pieno, ma l'orbita lascia vuoto l'angolo ovest: la zona (9.6-9.8) ci cade dentro
    area = {"w": 9.6, "s": 45.1, "e": 9.8, "n": 45.2}
    cut = _scene("tagliata", "2026-10-04", 3, bbox=(9.33, 45.0, 10.5, 46.0),
                 ring=[[10.5, 45.0], [10.5, 46.0], [10.2, 46.0], [9.7, 45.0], [10.5, 45.0]])
    full = _scene("intera", "2026-10-02", 36)
    assert not diag.covers(cut, area) and diag.covers(full, area)
    assert [f["id"] for f in diag.rank_scenes([cut, full], area, date(2026, 10, 11))] == ["intera"]


def test_validazione_configurazione_diagnostica():
    from ots_milsim_companion_plugin.default_config import DefaultConfig

    ok = {
        "OTS_MILSIM_DIAGNOSTICS": True, "OTS_MILSIM_DIAG_PRESET": "LONG_FAST", "OTS_MILSIM_DIAG_TX_DBM": 22,
        "OTS_MILSIM_DIAG_ANTENNA_DBI": 0, "OTS_MILSIM_DIAG_BODY_LOSS_DB": 3, "OTS_MILSIM_DIAG_MARGIN_DB": 10,
        "OTS_MILSIM_DIAG_TAG_HEIGHT_M": 1.5, "OTS_MILSIM_DIAG_MAX_SCENE_DAYS": 20, "OTS_MILSIM_HQ_NODE_ID": "",
        "OTS_MILSIM_HQ_LAT": 44.93, "OTS_MILSIM_HQ_LON": 9.56, "OTS_MILSIM_HQ_HEIGHT_M": 3,
    }
    assert DefaultConfig.validate(ok)["success"]
    for key, bad in (("OTS_MILSIM_DIAG_PRESET", "SHORT_TURBO"), ("OTS_MILSIM_DIAG_TX_DBM", 99),
                     ("OTS_MILSIM_HQ_NODE_ID", "gateway"), ("OTS_MILSIM_DIAGNOSTICS", "si"),
                     ("OTS_MILSIM_DIAG_MAX_SCENE_DAYS", True)):
        assert not DefaultConfig.validate({**ok, key: bad})["success"], key
    assert DefaultConfig.validate({**ok, "OTS_MILSIM_HQ_NODE_ID": "!a1b2c3d4"})["success"]
