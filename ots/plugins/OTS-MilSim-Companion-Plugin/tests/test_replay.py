"""Replay: a chi appartiene ogni riga di `points`.

Le righe sono (t, uid, device_uid, lat, lon, speed) come le scrive OTS:
cot_parser mette in device_uid il MITTENTE (il telefono anche per un tag
rilanciato), il meshtastic_controller mette il nodo e un uid casuale.
"""

from ots_milsim_companion_plugin import replay

ALPHA, BRAVO, OBS = 1, 2, 3
ROLES = {"team_a": ALPHA, "team_b": BRAVO, "observers": [OBS]}

PHONE = "ANDROID-aaaa"
TAG = {
    "tag_key": "!bbad0ac8", "node_id": "!bbad0ac8", "cot_uid": "TRACKER-01",
    "callsign": "T-ALFA-1", "long_name": None, "short_name": "0ac8", "owner_user_id": 20,
}
MESH_EUD = {"uid": "bbad0ac8", "callsign": "Meshtastic 0ac8", "meshtastic_id": 0xBBAD0AC8, "user_id": None}

TEAMS = {10: [(ALPHA, "IN"), (ALPHA, "OUT"), (OBS, "IN")], 20: [(BRAVO, "IN"), (BRAVO, "OUT"), (OBS, "IN")]}


def team_of(user_id):
    return replay.user_team(TEAMS.get(user_id, []), ROLES)


def current_team_at(owner, t):
    """Nessuno storico: squadra dallo stato attuale (TEAMS, tag di TAG)."""
    user_id = 20 if owner.get("tag_key") == TAG["tag_key"] else owner.get("user_id")
    return (team_of(user_id) if user_id else None), False


def build(rows, tags=(TAG,), mesh_euds=(MESH_EUD,), relay_uids=(), step=0, team_at=current_team_at):
    index = replay.TagIndex(list(tags), list(mesh_euds), relay_uids)
    rows = [r if len(r) == 7 else (*r, None) for r in rows]   # course non rilevante qui
    return replay.build_tracks(rows, index, {PHONE: ("ALFA-1", 10)}, team_at, step)


def test_squadra_utente():
    assert team_of(10) == replay.TEAM_A
    assert team_of(20) == replay.TEAM_B
    # IN sugli osservatori ce l'hanno anche i giocatori: osservatore solo con OUT
    assert replay.user_team([(OBS, "IN")], ROLES) is None
    assert replay.user_team([(OBS, "IN"), (OBS, "OUT")], ROLES) == replay.OBSERVERS


def test_tag_rilanciato_non_finisce_nella_traccia_del_telefono():
    tracks = build([
        (100.0, PHONE, PHONE, 45.1, 9.1, 0),
        (101.0, "TRACKER-01", PHONE, 45.2, 9.2, 0),   # relay ATAK (Path A)
    ])
    by_uid = {t["uid"]: t for t in tracks}
    assert by_uid[PHONE]["points"] == [[100.0, 45.1, 9.1, 0, None]]
    tag = by_uid["tag:!bbad0ac8"]
    assert tag["kind"] == "tag" and tag["callsign"] == "T-ALFA-1"
    assert tag["team"] == replay.TEAM_B       # squadra del proprietario, non del telefono
    assert by_uid[PHONE]["team"] == replay.TEAM_A


def test_relay_e_mqtt_dello_stesso_tag_sono_una_traccia():
    tracks = build([
        (100.0, "TRACKER-01", PHONE, 45.2, 9.2, 0),
        (110.0, "0b5e3c2a-uuid-casuale", "bbad0ac8", 45.3, 9.3, 1.5),   # MQTT (Path B)
    ])
    assert len(tracks) == 1
    assert [p[0] for p in tracks[0]["points"]] == [100.0, 110.0]


def test_marker_piazzato_dal_telefono_scartato():
    tracks = build([
        (100.0, PHONE, PHONE, 45.1, 9.1, 0),
        (101.0, "marker-uuid", PHONE, 45.9, 9.9, 0),
        (102.0, "marker-server", None, 45.9, 9.9, 0),
    ])
    assert [t["uid"] for t in tracks] == [PHONE]
    assert len(tracks[0]["points"]) == 1


def test_tag_sconosciuto_al_plugin():
    # Visto solo via MQTT: riga euds Meshtastic, nessun msh_tags
    tracks = build([(100.0, "uuid", "deadbeef", 45.0, 9.0, 0)], tags=(),
                   mesh_euds=({"uid": "deadbeef", "callsign": "Meshtastic beef", "meshtastic_id": 0xDEADBEEF, "user_id": None},))
    assert tracks[0]["kind"] == "tag" and tracks[0]["callsign"] == "Meshtastic beef"
    assert tracks[0]["team"] is None
    # Visto solo via relay ATAK: lo riconosce il <__meshtastic/> del CoT
    tracks = build([(100.0, "TRK-X", PHONE, 45.0, 9.0, 0)], tags=(), mesh_euds=(), relay_uids=["TRK-X"])
    assert tracks[0]["kind"] == "tag" and tracks[0]["uid"] == "tag:TRK-X"


def test_downsampling_e_ordine_per_squadra():
    tracks = build([
        (100.0, "TRACKER-01", PHONE, 45.2, 9.2, 0),
        (102.0, "TRACKER-01", PHONE, 45.2, 9.2, 0),   # copia ravvicinata (due telefoni)
        (106.0, "TRACKER-01", PHONE, 45.2, 9.2, 0),
        (100.0, PHONE, PHONE, 0.0, 0.0, 0),           # nessun fix
        (101.0, PHONE, PHONE, 45.1, 9.1, 0),
    ], step=5)
    assert [t["team"] for t in tracks] == [replay.TEAM_A, replay.TEAM_B]
    assert len(tracks[0]["points"]) == 1
    assert [p[0] for p in tracks[1]["points"]] == [100.0, 106.0]


# ----------------------------------------------------------------------
# Storico squadre
# ----------------------------------------------------------------------

from ots_milsim_companion_plugin import history  # noqa: E402


def test_diff_chiude_e_apre():
    open_rows = {("group", "10", ALPHA, "IN"): 1, ("group", "10", ALPHA, "OUT"): 2}
    facts = history.current_facts(
        [(10, BRAVO, "IN"), (10, BRAVO, "OUT"), (10, ALPHA, "IN")],
        [("!bbad0ac8", 20), ("!libero", None)],
        ROLES,
    )
    to_close, to_open = history.diff(open_rows, facts)
    assert to_close == [2]
    assert ("group", "10", BRAVO, "OUT") in to_open and ("group", "10", ALPHA, "IN") not in to_open
    assert ("tag", "!bbad0ac8", 20, None) in to_open
    assert not any(f[1] == "!libero" for f in to_open)
    assert ("role", "team_a", ALPHA, None) in to_open and ("role", "observers", OBS, None) in to_open


def _timeline():
    # ALFA-1 (utente 10) passa da Alpha a Bravo a t=200; il tag passa da 20 a 10 a t=150
    rows = [
        ("group", "10", ALPHA, "IN", 0, 200), ("group", "10", ALPHA, "OUT", 0, 200),
        ("group", "10", BRAVO, "IN", 200, None), ("group", "10", BRAVO, "OUT", 200, None),
        ("group", "20", BRAVO, "IN", 0, None), ("group", "20", BRAVO, "OUT", 0, None),
        ("tag", "!bbad0ac8", 20, None, 0, 150), ("tag", "!bbad0ac8", 10, None, 150, None),
        ("role", "team_a", ALPHA, None, 0, None), ("role", "team_b", BRAVO, None, 0, None),
    ]
    return history.Timeline(rows, since=0)


def test_squadra_al_momento_del_punto():
    tl = _timeline()
    team_at = replay.team_resolver(tl, {}, {}, ROLES)
    assert team_at({"user_id": 10}, 100) == (replay.TEAM_A, True)
    assert team_at({"user_id": 10}, 250) == (replay.TEAM_B, True)
    assert team_at({"tag_key": "!bbad0ac8", "user_id": None}, 100) == (replay.TEAM_B, True)
    assert team_at({"tag_key": "!bbad0ac8", "user_id": None}, 160) == (replay.TEAM_A, True)


def test_prima_dello_storico_usa_lo_stato_attuale():
    tl = history.Timeline([
        ("group", "10", BRAVO, "OUT", 1000, None),
        ("role", "team_a", ALPHA, None, 1000, None), ("role", "team_b", BRAVO, None, 1000, None),
    ], since=1000)
    team_at = replay.team_resolver(tl, {10: [(ALPHA, "IN")]}, {}, ROLES)
    assert team_at({"user_id": 10}, 500) == (replay.TEAM_A, False)
    assert team_at({"user_id": 10}, 1500) == (replay.TEAM_B, True)


def test_squadra_prevalente_e_partita_con_squadre_proprie():
    team_at = replay.team_resolver(_timeline(), {}, {}, ROLES)
    rows = [(t, PHONE, PHONE, 45.1, 9.1, 0) for t in (100, 150, 190, 250)]
    track = build(rows, team_at=team_at)[0]
    assert track["team"] == replay.TEAM_A and track["team_from_current"] is False
    # Replay partita: valgono i gruppi salvati nella partita, non la mappatura dello storico
    swapped = replay.team_resolver(_timeline(), {}, {}, ROLES, match_roles={"team_a": BRAVO, "team_b": ALPHA, "observers": []})
    assert build(rows, team_at=swapped)[0]["team"] == replay.TEAM_B


# ----------------------------------------------------------------------
# Direzione di marcia
# ----------------------------------------------------------------------

# ~11 m verso nord / est a queste latitudini
NORTH = 0.0001
EAST = 0.00014


def _pts(*coords, speed=None, dt=5):
    return [[i * dt, lat, lon, speed] for i, (lat, lon) in enumerate(coords)]


def test_direzione_dallo_spostamento():
    pts = _pts((45.0, 9.0), (45.0 + NORTH, 9.0), (45.0 + NORTH, 9.0 + EAST))
    assert replay.headings(pts, [None] * 3) == [None, 0, 90]


def test_da_fermo_resta_l_ultima_direzione_buona():
    # Cammina verso nord, poi il fix balla di 1-2 m in tutte le direzioni
    pts = _pts((45.0, 9.0), (45.0 + NORTH, 9.0),
               (45.0 + NORTH + 0.00001, 9.0 - 0.00002), (45.0 + NORTH - 0.00001, 9.0 + 0.00001))
    assert replay.headings(pts, [None] * 4) == [None, 0, 0, 0]


def test_course_del_gps_solo_in_movimento():
    pts = _pts((45.0, 9.0), (45.0, 9.0), (45.0, 9.0), (45.0, 9.0))
    for p, speed in zip(pts, (0.0, 1.5, 0.2, 1.5)):
        p[3] = speed
    # 200° a 1,5 m/s vale; 45° a 0,2 m/s è rumore; course 0 = «non disponibile»
    assert replay.headings(pts, [None, 200.0, 45.0, 0.0]) == [None, 200, 200, 200]


def test_niente_direzione_attraverso_un_buco():
    pts = _pts((45.0, 9.0), (45.0 + NORTH * 20, 9.0), dt=600)
    assert replay.headings(pts, [None, None]) == [None, None]


def test_build_tracks_aggiunge_la_direzione():
    tracks = build([(0.0, PHONE, PHONE, 45.0, 9.0, 0, None), (5.0, PHONE, PHONE, 45.0 + NORTH, 9.0, 0, None)])
    assert [p[4] for p in tracks[0]["points"]] == [None, 0]
