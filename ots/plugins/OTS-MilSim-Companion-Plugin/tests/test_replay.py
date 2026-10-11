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


def build(rows, tags=(TAG,), mesh_euds=(MESH_EUD,), relay_uids=(), step=0):
    index = replay.TagIndex(list(tags), list(mesh_euds), relay_uids)
    return replay.build_tracks(rows, index, {PHONE: ("ALFA-1", 10)}, team_of, step)


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
    assert by_uid[PHONE]["points"] == [[100.0, 45.1, 9.1, 0]]
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
