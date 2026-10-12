"""Modello di portata LoRa degli strumenti diagnostici del replay."""

import math

import pytest

from ots_milsim_companion_plugin import radio


def test_budget_preset_corretto_per_potenza_corpo_e_margine():
    # Long Fast 153 dB a 22 dBm; T-Beam a 20 dBm, 3 dB di corpo per lato, 10 dB di margine
    assert radio.allowed_loss("LONG_FAST", 20, 0, 3, 10) == pytest.approx(153 - 2 - 6 - 10)
    # Verso il quartier generale c'è un solo corpo di mezzo
    assert radio.allowed_loss("LONG_FAST", 22, 0, 3, 10, bodies=1) == pytest.approx(140)


def test_due_raggi_continuo_al_punto_di_rottura():
    dc = radio.breakpoint_m(1.5, 1.5)
    assert 80 < dc < 85                       # ~82 m a 869 MHz con due antenne a 1,5 m
    assert radio.path_loss(dc - 0.01, 1.5, 1.5) == pytest.approx(radio.path_loss(dc + 0.01, 1.5, 1.5), abs=0.01)
    # Oltre d_c: 12 dB in più raddoppiando la distanza (40 dB/decade), non 6
    assert radio.path_loss(2000, 1.5, 1.5) - radio.path_loss(1000, 1.5, 1.5) == pytest.approx(40 * math.log10(2))


def test_portata_in_campo_aperto():
    allowed = radio.allowed_loss("LONG_FAST", 22, 0, 3, 10)        # 137 dB
    d = radio.open_range_m(allowed, 1.5, 1.5)
    assert radio.path_loss(d, 1.5, 1.5) == pytest.approx(allowed)
    assert 3500 < d < 4500
    # Preset più corto, portata più corta; antenna più alta, portata più lunga
    assert radio.open_range_m(radio.allowed_loss("SHORT_FAST", 22, 0, 3, 10), 1.5, 1.5) < d
    assert radio.open_range_m(allowed, 1.5, 6.0) > d
    # Budget così piccolo da stare sotto il punto di rottura: vale lo spazio libero
    small = radio.free_space_loss(50)
    assert radio.open_range_m(small, 1.5, 1.5) == pytest.approx(50, rel=1e-6)


def test_weissberger():
    f = (radio.FREQ_MHZ / 1000) ** 0.284
    assert radio.vegetation_loss(0) == 0
    assert radio.vegetation_loss(10) == pytest.approx(0.45 * f * 10)
    assert radio.vegetation_loss(100) == pytest.approx(1.33 * f * 100 ** 0.588)
    assert 17 < radio.vegetation_loss(100) < 21     # ~19 dB per 100 m di bosco


def test_densita_dalla_copertura_e_dall_ndvi():
    assert radio.foliage_density(radio.GRASS, 0.8) == 0            # prato verde: niente ostacolo
    assert radio.foliage_density(radio.TREE, 0.75) == 1.0          # bosco in foglia
    assert radio.foliage_density(radio.TREE, 0.1) == pytest.approx(0.35)   # bosco spoglio: rami e tronchi
    assert radio.foliage_density(radio.CROP, 0.4) == 0             # coltura bassa o raccolta
    assert radio.foliage_density(radio.CROP, 0.85) == pytest.approx(0.5)   # mais alto d'estate
    assert radio.foliage_density(radio.BUILT, None) == 1.0
    assert radio.foliage_density(radio.WATER, 0.9) == 0


def test_raggio_nel_bosco_si_accorcia():
    allowed = radio.allowed_loss("LONG_FAST", 22, 0, 3, 10)
    open_d = radio.reach_m(allowed, 1.5, 1.5, lambda d: 0.0)
    assert open_d == pytest.approx(radio.open_range_m(allowed, 1.5, 1.5))
    forest = radio.reach_m(allowed, 1.5, 1.5, lambda d: 1.0)
    assert forest < open_d / 2
    # Bosco solo nei primi 200 m: peggio del campo aperto, meglio del bosco continuo
    edge = radio.reach_m(allowed, 1.5, 1.5, lambda d: 1.0 if d < 200 else 0.0)
    assert forest < edge < open_d


def test_bolla_senza_vegetazione_e_un_cerchio():
    allowed = radio.allowed_loss("MEDIUM_FAST", 22, 0, 3, 10)
    r = radio.open_range_m(allowed, 1.5, 1.5)
    pts = radio.bubble(45.16, 9.70, allowed, 1.5, 1.5, lambda lat, lon: 0.0, rays=8)
    assert len(pts) == 8
    for lat, lon in pts:
        dy = (lat - 45.16) * math.pi / 180 * radio.EARTH_RADIUS_M
        dx = (lon - 9.70) * math.pi / 180 * radio.EARTH_RADIUS_M * math.cos(math.radians(45.16))
        assert math.hypot(dx, dy) == pytest.approx(r, rel=0.01)


def test_bolla_schiacciata_verso_il_bosco():
    allowed = radio.allowed_loss("LONG_FAST", 22, 0, 3, 10)
    # Bosco a nord dell'operatore, campo aperto altrove
    pts = radio.bubble(45.16, 9.70, allowed, 1.5, 1.5, lambda lat, lon: 1.0 if lat > 45.1605 else 0.0, rays=4)
    north, east = pts[0], pts[1]
    assert north[0] - 45.16 < (east[1] - 9.70) * math.cos(math.radians(45.16))
