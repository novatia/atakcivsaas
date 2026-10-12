"""Raggio minimo delle aree di gioco: sotto i 20 m il GPS dei tag decide a caso."""

import math

from ots_milsim_companion_plugin import game_modes

LAT0, LON0 = 45.15, 9.70
M_LAT = 1 / (math.pi / 180 * 6371000)
M_LON = M_LAT / math.cos(math.radians(LAT0))


def square(side_m):
    h = side_m / 2
    return [[LAT0 + dy * h * M_LAT, LON0 + dx * h * M_LON] for dx, dy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]


def rect(w_m, h_m):
    return [[LAT0 + dy * h_m / 2 * M_LAT, LON0 + dx * w_m / 2 * M_LON] for dx, dy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]


def bomb_template(zone_points):
    markers = [
        {"type": "spawn_a", "lat": LAT0, "lon": LON0},
        {"type": "spawn_b", "lat": LAT0, "lon": LON0},
        {"type": "bomb_site_a", "lat": LAT0, "lon": LON0},
        {"type": "bomb_site_b", "lat": LAT0, "lon": LON0},
    ]
    return markers, [{"type": "bomb_area", "points": zone_points}]


def test_inner_radius_of_square_is_half_side():
    assert abs(game_modes.inner_radius_m(square(100)) - 50) < 1


def test_long_thin_area_has_small_radius():
    # 200 x 20 m: 4000 m² ma ci sta solo un cerchio di 10 m
    assert abs(game_modes.inner_radius_m(rect(200, 20)) - 10) < 1


def test_concave_area():
    # L con bracci larghi 30 m: il cerchio massimo sta nell'angolo, raggio > 15
    p = [[0, 0], [0, 100], [30, 100], [30, 30], [100, 30], [100, 0]]
    pts = [[LAT0 + y * M_LAT, LON0 + x * M_LON] for x, y in p]
    r = game_modes.inner_radius_m(pts)
    assert 15 < r < 30


def test_degenerate_area():
    assert game_modes.inner_radius_m([[LAT0, LON0]] * 3) == 0


def test_small_bomb_area_rejected_on_save_and_play():
    markers, zones = bomb_template(square(30))
    for for_play in (False, True):
        errors = game_modes.validate_template("bomb", markers, zones, for_play=for_play)
        assert any("troppo piccola" in e and "20 m" in e for e in errors)


def test_area_at_minimum_accepted():
    markers, zones = bomb_template(square(42))
    assert game_modes.validate_template("bomb", markers, zones, for_play=True) == []


def test_field_boundary_has_no_minimum():
    errors = game_modes.validate_template("tdm", [], [{"type": "field_boundary", "points": square(10)}])
    assert errors == []
