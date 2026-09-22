"""Test dell'integrazione Meshtastic / TAK tracker.

Copre la lista del capitolato: rilevamento, posizione, mappatura esplicita,
canali multipli senza incroci, canale sconosciuto con fallback, relay ATAK
(identità del tracker distinta dall'EUD), ricezione duplicata da più gateway,
invecchiamento LIVE→RECENT→STALE, isolamento fra gruppi e sanificazione.

I CoT usati come input riproducono esattamente quelli generati da
`MeshtasticReceiver.java` del plugin Meshtastic per ATAK e da
`meshtastic_controller.py` di OpenTAKServer.
"""

import json
from datetime import timedelta

import pytest

from conftest import FakeChannel, Mapping
from ots_milsim_companion_plugin import mesh


# ----------------------------------------------------------------------
# CoT di esempio
# ----------------------------------------------------------------------

def relay_cot(uid="ALPHA-1", callsign="ALPHA-1", lat=45.123456, lon=9.654321,
              time="2026-09-22T20:34:12Z", battery=33):
    """PLI rilanciato dal plugin Meshtastic di ATAK ("Relay to Server").

    Struttura presa dal sorgente: `<__meshtastic/>` VUOTO, nessun canale,
    nessun node id, `hae` non disponibile.
    """
    return (
        f'<event version="2.0" uid="{uid}" type="a-f-G-U-C" how="m-g" '
        f'time="{time}" start="{time}" stale="2026-09-22T20:44:12Z">'
        f'<point lat="{lat}" lon="{lon}" hae="9999999.0" ce="9999999.0" le="9999999.0"/>'
        f'<detail>'
        f'<contact callsign="{callsign}" endpoint="0.0.0.0:4242:tcp"/>'
        f'<__group role="Team Member" name="Cyan"/>'
        f'<status battery="{battery}"/>'
        f'<track speed="0" course="0"/>'
        f'<__meshtastic/>'
        f'</detail></event>'
    )


def native_cot(uid="bbad0ac8", callsign="ALPHA-2", lat=45.2, lon=9.7,
               time="2026-09-22T20:34:14Z"):
    """CoT generato dal meshtastic_controller di OTS dal feed MQTT."""
    return (
        f'<event version="2.0" uid="{uid}" type="a-f-G-U-C" how="m-g" '
        f'time="{time}" start="{time}" stale="2026-09-23T20:34:14Z">'
        f'<point lat="{lat}" lon="{lon}" hae="120.0" ce="9999999.0" le="9999999.0"/>'
        f'<detail>'
        f'<takv device="TBEAM" version="2.7.6" platform="Meshtastic" os="Meshtastic" '
        f'macaddr="" meshtastic_id="{uid}"/>'
        f'<contact callsign="{callsign}" endpoint="MQTT"/>'
        f'<uid Droid="{callsign}"/>'
        f'<precisionlocation altsrc="GPS" geopointsrc="GPS"/>'
        f'<status battery="77"/>'
        f'<track course="0.0" speed="0.0"/>'
        f'<__group name="Cyan" role="Team Member"/>'
        f'</detail></event>'
    )


def eud_self_cot(uid="ANDROID-abc", callsign="ALPHA"):
    """PLI normale dell'EUD ATAK: NON è Meshtastic, non deve essere raccolto."""
    return (
        f'<event version="2.0" uid="{uid}" type="a-f-G-U-C" how="m-g" '
        f'time="2026-09-22T20:34:10Z" start="2026-09-22T20:34:10Z" stale="2026-09-22T20:44:10Z">'
        f'<point lat="45.0" lon="9.0" hae="100.0" ce="10.0" le="10.0"/>'
        f'<detail><takv device="Pixel" platform="ATAK-CIV" os="34" version="5.1"/>'
        f'<contact callsign="{callsign}" endpoint="*:-1:stcp"/>'
        f'<__group role="Team Lead" name="Cyan"/></detail></event>'
    )


def firehose(cot_xml, sender_uid):
    """Il corpo che eud_handler mette sul firehose: uid = EUD che ha inviato."""
    return json.dumps({"uid": sender_uid, "cot": cot_xml}).encode()


# ----------------------------------------------------------------------
# 1. Rilevamento del tag e identità stabile
# ----------------------------------------------------------------------

def test_relay_cot_riconosciuto_come_meshtastic():
    descriptor = mesh.detect(relay_cot(), "ANDROID-abc")
    assert descriptor is not None
    assert descriptor["source"] == mesh.SOURCE_ATAK_RELAY
    assert descriptor["uid"] == "ALPHA-1"
    assert descriptor["callsign"] == "ALPHA-1"
    # Il relay ATAK non porta il canale: deve risultare assente, non zero
    assert descriptor["channel_index"] is None
    assert descriptor["channel_name"] is None
    assert descriptor["channel_metadata_present"] is False
    assert descriptor["node_id"] is None


def test_cot_normale_di_un_eud_non_e_un_tag():
    assert mesh.detect(eud_self_cot(), "ANDROID-abc") is None


def test_cot_nativo_ots_riconosciuto_con_node_id():
    descriptor = mesh.detect(native_cot(), None)
    assert descriptor["source"] == mesh.SOURCE_OTS_MESHTASTIC
    assert descriptor["node_id"] == "!bbad0ac8"
    assert descriptor["callsign"] == "ALPHA-2"


def test_geochat_non_e_un_tag():
    chat = (
        '<event version="2.0" uid="GeoChat.ALPHA.All Chat Rooms.x" type="b-t-f" how="h-g-i-g-o" '
        'time="2026-09-22T20:34:12Z" start="2026-09-22T20:34:12Z" stale="2026-09-22T20:44:12Z">'
        '<point lat="0" lon="0" hae="0" ce="9999999.0" le="9999999.0"/>'
        '<detail><__meshtastic/></detail></event>'
    )
    assert mesh.detect(chat, "ANDROID-abc") is None


def test_identita_stabile_fra_ricezioni(env):
    channel = FakeChannel()
    for _ in range(3):
        mesh.handle_cot(channel, firehose(relay_cot(), "ANDROID-abc"))
    assert list(mesh.REGISTRY.tags) == ["ALPHA-1"]
    assert mesh.REGISTRY.tags["ALPHA-1"].rx_count == 3


def test_node_id_preferito_come_identita(env):
    """Se il node id compare (via MQTT) il tag già noto per uid ci migra sopra,
    senza sdoppiarsi."""
    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(relay_cot(uid="!bbad0ac8"), "ANDROID-abc"))
    assert "!bbad0ac8" in mesh.REGISTRY.tags
    mesh.handle_cot(channel, firehose(native_cot(uid="bbad0ac8"), None))
    assert list(mesh.REGISTRY.tags) == ["!bbad0ac8"]


@pytest.mark.parametrize(
    "raw,expected",
    [("!bbad0ac8", "!bbad0ac8"), ("bbad0ac8", "!bbad0ac8"), (3148679880, "!bbad0ac8"), ("", None), (None, None)],
)
def test_normalizzazione_node_id(raw, expected):
    assert mesh.normalize_node_id(raw) == expected


# ----------------------------------------------------------------------
# 2. Posizione
# ----------------------------------------------------------------------

def test_posizione_conservata_senza_modifiche(env):
    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(relay_cot(lat=45.123456, lon=9.654321), "ANDROID-abc"))
    tag = mesh.REGISTRY.tags["ALPHA-1"]
    assert tag.latitude == 45.123456
    assert tag.longitude == 9.654321
    assert tag.gps == "fix"
    assert tag.battery == 33
    # hae 9999999.0 = non disponibile: non si inventa un'altitudine
    assert tag.altitude is None


def test_posizione_a_precisione_ridotta_non_viene_ritoccata(env):
    """Meshtastic può troncare di proposito le coordinate: si tiene com'è."""
    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(relay_cot(lat=45.12, lon=9.65), "ANDROID-abc"))
    tag = mesh.REGISTRY.tags["ALPHA-1"]
    assert tag.latitude == 45.12 and tag.longitude == 9.65


def test_senza_fix_gps_lo_stato_e_none(env):
    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(relay_cot(lat=0.0, lon=0.0), "ANDROID-abc"))
    tag = mesh.REGISTRY.tags["ALPHA-1"]
    assert tag.latitude is None
    assert tag.gps_status(120) == "none"


def test_gps_diventa_stale_senza_aggiornamenti(env):
    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(relay_cot(), "ANDROID-abc"))
    tag = mesh.REGISTRY.tags["ALPHA-1"]
    assert tag.gps_status(120) == "fix"
    tag.last_position -= timedelta(seconds=200)
    assert tag.gps_status(120) == "stale"


# ----------------------------------------------------------------------
# 3-4. Mappatura esplicita del canale, niente incroci fra canali
# ----------------------------------------------------------------------

def test_mappatura_esplicita_instrada_al_gruppo(env):
    env["mappings"] = [Mapping(group_id=1, channel_name="ALPHA", channel_index=0)]
    env["overrides"] = {"ALPHA-1": {"manual_channel_name": "ALPHA"}}
    env["eud_groups"] = {"ANDROID-abc": [3]}  # l'EUD relay sta in LOGISTICS

    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(relay_cot(), "ANDROID-abc"))

    trace = mesh.REGISTRY.tags["ALPHA-1"].last_routing
    assert trace["result"] == mesh.RESULT_ROUTED
    assert trace["group_name"] == "ALPHA"
    assert channel.published == [
        {
            "exchange": "groups",
            "routing_key": "ALPHA.OUT",
            "body": channel.published[0]["body"],
        }
    ]


def test_canali_diversi_non_si_incrociano(env):
    env["mappings"] = [
        Mapping(group_id=1, channel_name="ALPHA"),
        Mapping(group_id=2, channel_name="BRAVO"),
    ]
    env["overrides"] = {
        "ALPHA-1": {"manual_channel_name": "ALPHA"},
        "BRAVO-1": {"manual_channel_name": "BRAVO"},
    }
    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(relay_cot(uid="ALPHA-1", callsign="ALPHA-1"), "ANDROID-abc"))
    mesh.handle_cot(channel, firehose(relay_cot(uid="BRAVO-1", callsign="BRAVO-1"), "ANDROID-abc"))

    keys = [p["routing_key"] for p in channel.published]
    assert keys == ["ALPHA.OUT", "BRAVO.OUT"]
    # Nessun CoT di ALPHA-1 è finito su BRAVO e viceversa
    alpha_body = json.loads(channel.published[0]["body"])
    bravo_body = json.loads(channel.published[1]["body"])
    assert alpha_body["uid"] == "ALPHA-1" and "ALPHA-1" in alpha_body["cot"]
    assert bravo_body["uid"] == "BRAVO-1" and "ALPHA-1" not in bravo_body["cot"]


def test_mappatura_per_indice_di_canale(env):
    env["mappings"] = [Mapping(group_id=2, channel_index=1)]
    env["overrides"] = {"ALPHA-1": {"manual_channel_index": 1}}
    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(relay_cot(), "ANDROID-abc"))
    assert channel.published[0]["routing_key"] == "BRAVO.OUT"


def test_mappatura_disabilitata_non_instrada(env):
    env["mappings"] = [Mapping(group_id=1, channel_name="ALPHA", enabled=False)]
    env["overrides"] = {"ALPHA-1": {"manual_channel_name": "ALPHA"}}
    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(relay_cot(), "ANDROID-abc"))
    assert channel.published == []


def test_gruppo_forzato_sul_tag_ha_la_precedenza(env):
    env["mappings"] = [Mapping(group_id=1, channel_name="ALPHA")]
    env["overrides"] = {"ALPHA-1": {"manual_channel_name": "ALPHA", "manual_group_id": 3}}
    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(relay_cot(), "ANDROID-abc"))
    assert channel.published[0]["routing_key"] == "LOGISTICS.OUT"


# ----------------------------------------------------------------------
# 5. Canale sconosciuto -> fallback
# ----------------------------------------------------------------------

def test_canale_sconosciuto_fallback_gruppo_eud(env):
    env["eud_groups"] = {"ANDROID-abc": [1]}
    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(relay_cot(), "ANDROID-abc"))

    trace = mesh.REGISTRY.tags["ALPHA-1"].last_routing
    assert trace["channel_name"] is None and trace["channel_index"] is None
    assert trace["fallback"] == "source_eud_group"
    assert trace["result"] == mesh.RESULT_NATIVE_ONLY
    assert trace["group_name"] == "ALPHA"
    # OTS instrada già lui: il plugin non ripubblica nulla
    assert channel.published == []
    assert "ALPHA-1" in mesh.REGISTRY.unknown_channel_keys


def test_canale_sconosciuto_fallback_gruppo_default(env):
    env["config"]["OTS_MILSIM_MESH_FALLBACK_POLICY"] = "default_group"
    env["config"]["OTS_MILSIM_MESH_DEFAULT_GROUP_ID"] = 2
    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(relay_cot(), "ANDROID-abc"))
    trace = mesh.REGISTRY.tags["ALPHA-1"].last_routing
    assert trace["result"] == mesh.RESULT_ROUTED_FALLBACK
    assert channel.published[0]["routing_key"] == "BRAVO.OUT"


def test_canale_sconosciuto_fallback_gruppo_meshtastic(env):
    env["config"]["OTS_MILSIM_MESH_FALLBACK_POLICY"] = "meshtastic_group"
    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(relay_cot(), "ANDROID-abc"))
    assert channel.published[0]["routing_key"] == "Meshtastic.OUT"


def test_canale_sconosciuto_politica_ignore(env):
    env["config"]["OTS_MILSIM_MESH_FALLBACK_POLICY"] = "ignore"
    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(relay_cot(), "ANDROID-abc"))
    trace = mesh.REGISTRY.tags["ALPHA-1"].last_routing
    assert trace["result"] == mesh.RESULT_IGNORED
    assert channel.published == []


def test_fallback_default_senza_gruppo_configurato_e_un_errore(env):
    env["config"]["OTS_MILSIM_MESH_FALLBACK_POLICY"] = "default_group"
    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(relay_cot(), "ANDROID-abc"))
    assert mesh.REGISTRY.tags["ALPHA-1"].last_routing["result"] == mesh.RESULT_ERROR
    assert mesh.REGISTRY.routing_errors >= 1


def test_il_canale_non_viene_mai_dedotto_dal_callsign(env):
    """Il tag si chiama ALPHA-1 e ALPHA è un canale mappato: non basta."""
    env["mappings"] = [Mapping(group_id=1, channel_name="ALPHA")]
    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(relay_cot(), "ANDROID-abc"))
    trace = mesh.REGISTRY.tags["ALPHA-1"].last_routing
    assert trace["mapping"] is None
    assert trace["fallback"] == "source_eud_group"


# ----------------------------------------------------------------------
# 6. Relay ATAK: il tracker non è l'EUD
# ----------------------------------------------------------------------

def test_relay_atak_identita_distinta_dallo_eud(env):
    """ALPHA rilancia ALPHA-1 e ALPHA-2: tre entità, non una."""
    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(eud_self_cot(uid="ANDROID-abc", callsign="ALPHA"), "ANDROID-abc"))
    mesh.handle_cot(channel, firehose(relay_cot(uid="ALPHA-1", callsign="ALPHA-1"), "ANDROID-abc"))
    mesh.handle_cot(channel, firehose(relay_cot(uid="ALPHA-2", callsign="ALPHA-2"), "ANDROID-abc"))

    # Il PLI dell'EUD non entra nel registry: non è un tag Meshtastic
    assert sorted(mesh.REGISTRY.tags) == ["ALPHA-1", "ALPHA-2"]
    for key in ("ALPHA-1", "ALPHA-2"):
        tag = mesh.REGISTRY.tags[key]
        assert tag.callsign == key                      # callsign del tracker, non dell'EUD
        assert tag.uid == key                           # uid stabile del tracker
        assert tag.last_routing["source_eud"] == "ANDROID-abc"
        assert list(tag.paths) == ["ANDROID-abc"]


# ----------------------------------------------------------------------
# 7. Ricezione duplicata da più gateway
# ----------------------------------------------------------------------

def test_stesso_tag_da_due_relay_resta_un_solo_oggetto(env):
    channel = FakeChannel()
    cot = relay_cot(time="2026-09-22T20:34:12Z")
    mesh.handle_cot(channel, firehose(cot, "ANDROID-A"))
    mesh.handle_cot(channel, firehose(cot, "ANDROID-B"))

    assert list(mesh.REGISTRY.tags) == ["ALPHA-1"]
    tag = mesh.REGISTRY.tags["ALPHA-1"]
    assert sorted(tag.paths) == ["ANDROID-A", "ANDROID-B"]
    assert tag.duplicate_count == 1  # stesso uid + stesso time = stesso pacchetto


def test_duplicato_non_viene_ripubblicato(env):
    env["mappings"] = [Mapping(group_id=1, channel_name="ALPHA")]
    env["overrides"] = {"ALPHA-1": {"manual_channel_name": "ALPHA"}}
    channel = FakeChannel()
    cot = relay_cot(time="2026-09-22T20:34:12Z")
    mesh.handle_cot(channel, firehose(cot, "ANDROID-A"))
    mesh.handle_cot(channel, firehose(cot, "ANDROID-B"))
    assert len(channel.published) == 1


def test_aggiornamento_piu_recente_non_viene_soppresso(env):
    """Una posizione nuova non è un duplicato, anche dallo stesso gateway."""
    env["mappings"] = [Mapping(group_id=1, channel_name="ALPHA")]
    env["overrides"] = {"ALPHA-1": {"manual_channel_name": "ALPHA"}}
    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(relay_cot(time="2026-09-22T20:34:12Z", lat=45.1), "ANDROID-A"))
    mesh.handle_cot(channel, firehose(relay_cot(time="2026-09-22T20:34:22Z", lat=45.2), "ANDROID-B"))

    tag = mesh.REGISTRY.tags["ALPHA-1"]
    assert tag.latitude == 45.2          # vince la posizione più recente
    assert tag.duplicate_count == 0
    assert len(channel.published) == 2


# ----------------------------------------------------------------------
# 8. Invecchiamento LIVE -> RECENT -> STALE
# ----------------------------------------------------------------------

def test_stati_del_tag_nel_tempo(env):
    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(relay_cot(), "ANDROID-abc"))
    tag = mesh.REGISTRY.tags["ALPHA-1"]

    assert tag.status(60, 300) == "live"
    tag.last_seen -= timedelta(seconds=90)
    assert tag.status(60, 300) == "recent"
    tag.last_seen -= timedelta(seconds=300)
    assert tag.status(60, 300) == "stale"


def test_soglie_configurabili(env):
    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(relay_cot(), "ANDROID-abc"))
    tag = mesh.REGISTRY.tags["ALPHA-1"]
    tag.last_seen -= timedelta(seconds=10)
    assert tag.status(5, 3600) == "recent"   # soglie strette
    assert tag.status(3600, 7200) == "live"  # soglie larghe


# ----------------------------------------------------------------------
# 9. Isolamento fra gruppi
# ----------------------------------------------------------------------

def test_instradamento_usa_solo_exchange_groups(env):
    """Mai firehose, mai fanout, mai dms: solo `groups` con `<gruppo>.OUT`,
    cioè lo stesso meccanismo autorizzativo del cot_parser."""
    env["mappings"] = [Mapping(group_id=2, channel_name="BRAVO")]
    env["overrides"] = {"ALPHA-1": {"manual_channel_name": "BRAVO"}}
    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(relay_cot(), "ANDROID-abc"))

    assert len(channel.published) == 1
    published = channel.published[0]
    assert published["exchange"] == "groups"
    assert published["routing_key"] == "BRAVO.OUT"
    # Nessuna consegna a gruppi estranei
    assert all(p["routing_key"] == "BRAVO.OUT" for p in channel.published)


def test_nessuna_doppia_consegna_se_il_gruppo_e_gia_quello_dello_eud(env):
    """Se il gruppo mappato coincide con quello in cui OTS ha già instradato,
    ripubblicare significherebbe consegnare due volte lo stesso evento."""
    env["mappings"] = [Mapping(group_id=1, channel_name="ALPHA")]
    env["overrides"] = {"ALPHA-1": {"manual_channel_name": "ALPHA"}}
    env["eud_groups"] = {"ANDROID-abc": [1]}
    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(relay_cot(), "ANDROID-abc"))
    assert channel.published == []
    assert mesh.REGISTRY.tags["ALPHA-1"].last_routing["result"] == mesh.RESULT_ROUTED


def test_path_b_il_gruppo_gia_usato_da_ots_e_quello_meshtastic(env):
    """Per i CoT generati dal meshtastic_controller OTS usa un gruppo fisso
    (OTS_MESHTASTIC_GROUP), non i gruppi dell'EUD: il fallback deve dirlo."""
    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(native_cot(), None))
    trace = mesh.REGISTRY.tags["!bbad0ac8"].last_routing
    assert trace["native_groups"] == ["Meshtastic"]
    assert trace["result"] == mesh.RESULT_NATIVE_ONLY
    assert trace["group_name"] == "Meshtastic"
    assert channel.published == []


def test_path_b_mappatura_su_altro_gruppo_viene_instradata(env):
    """Il canale ALPHA mappato su ALPHA aggiunge una consegna che OTS non fa:
    nativamente finirebbe solo nel gruppo «Meshtastic»."""
    env["mappings"] = [Mapping(group_id=1, channel_name="ALPHA")]
    mesh.REGISTRY.node_channels["!bbad0ac8"] = {"name": "ALPHA", "index": 0}
    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(native_cot(), None))
    assert channel.published[0]["routing_key"] == "ALPHA.OUT"


def test_path_b_mappatura_sul_gruppo_meshtastic_non_duplica(env):
    """Se il canale e' mappato proprio sul gruppo dove OTS consegna gia', il
    plugin non ripubblica."""
    env["mappings"] = [Mapping(group_id=9, channel_name="ALPHA")]  # 9 = Meshtastic
    mesh.REGISTRY.node_channels["!bbad0ac8"] = {"name": "ALPHA", "index": 0}
    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(native_cot(), None))
    assert channel.published == []
    assert mesh.REGISTRY.tags["!bbad0ac8"].last_routing["result"] == mesh.RESULT_ROUTED


def test_eud_senza_gruppi_instrada_ad_anon_lato_ots(env):
    """Un EUD senza membership finisce in __ANON__ (route_cot di OTS): una
    mappatura verso un gruppo vero aggiunge quindi qualcosa."""
    env["mappings"] = [Mapping(group_id=2, channel_name="BRAVO")]
    env["overrides"] = {"ALPHA-1": {"manual_channel_name": "BRAVO"}}
    env["eud_groups"] = {}
    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(relay_cot(), "ANDROID-abc"))
    trace = mesh.REGISTRY.tags["ALPHA-1"].last_routing
    assert trace["native_groups"] == ["__ANON__"]
    assert channel.published[0]["routing_key"] == "BRAVO.OUT"


def test_mappatura_verso_gruppo_cancellato_non_instrada(env):
    env["mappings"] = [Mapping(group_id=42, channel_name="ALPHA")]
    env["overrides"] = {"ALPHA-1": {"manual_channel_name": "ALPHA"}}
    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(relay_cot(), "ANDROID-abc"))
    assert channel.published == []
    assert mesh.REGISTRY.tags["ALPHA-1"].last_routing["result"] == mesh.RESULT_ERROR


def test_errore_di_pubblicazione_registrato_e_non_nascosto(env):
    env["mappings"] = [Mapping(group_id=1, channel_name="ALPHA")]
    env["overrides"] = {"ALPHA-1": {"manual_channel_name": "ALPHA"}}
    channel = FakeChannel(fail=True)
    mesh.handle_cot(channel, firehose(relay_cot(), "ANDROID-abc"))
    tag = mesh.REGISTRY.tags["ALPHA-1"]
    assert tag.last_routing["result"] == mesh.RESULT_ERROR
    assert tag.last_error
    assert mesh.REGISTRY.routing_errors >= 1


# ----------------------------------------------------------------------
# 10. Sanificazione
# ----------------------------------------------------------------------

SECRET_COT = (
    '<event version="2.0" uid="ALPHA-1" type="a-f-G-U-C" how="m-g" '
    'time="2026-09-22T20:34:12Z" start="2026-09-22T20:34:12Z" stale="2026-09-22T20:44:12Z">'
    '<point lat="45.1" lon="9.1" hae="0" ce="9999999.0" le="9999999.0"/>'
    '<detail>'
    '<contact callsign="ALPHA-1"/>'
    '<__meshtastic channel_psk="1PG7OiApB1nwvP+rz05pAQ==" psk="AQ=="/>'
    '<mqtt password="hunter2" username="ots" api_key="sk-live-123"/>'
    '<auth_token>eyJhbGciOiJIUzI1NiJ9.super.secret</auth_token>'
    '<cookie>session=abc123</cookie>'
    '</detail></event>'
)
SECRETS = ["1PG7OiApB1nwvP+rz05pAQ==", "hunter2", "sk-live-123", "eyJhbGciOiJIUzI1NiJ9", "session=abc123"]


def test_sanificazione_rimuove_i_segreti():
    clean = mesh.sanitize_cot_xml(SECRET_COT)
    for secret in SECRETS:
        assert secret not in clean, f"segreto trapelato: {secret}"
    assert mesh.REDACTED in clean
    # I dati utili restano leggibili
    assert 'callsign="ALPHA-1"' in clean
    assert 'lat="45.1"' in clean


def test_il_cot_mostrato_nei_pacchetti_e_sanificato(env):
    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(SECRET_COT, "ANDROID-abc"))
    packet = mesh.REGISTRY.tags["ALPHA-1"].packets[0]
    for secret in SECRETS:
        assert secret not in packet["raw_cot"]


def test_xml_non_analizzabile_non_viene_mostrato_grezzo():
    clean = mesh.sanitize_cot_xml("<event psk='segreto' non chiuso")
    assert "segreto" not in clean


def test_la_sanificazione_non_altera_il_cot_instradato(env):
    """Il CoT consegnato agli EUD è quello originale: la sanificazione vale
    solo per la UI di debug, non deve alterare il traffico operativo."""
    env["mappings"] = [Mapping(group_id=1, channel_name="ALPHA")]
    env["overrides"] = {"ALPHA-1": {"manual_channel_name": "ALPHA"}}
    original = relay_cot()
    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(original, "ANDROID-abc"))
    assert json.loads(channel.published[0]["body"])["cot"] == original


# ----------------------------------------------------------------------
# Feed MQTT: topic, canale, correlazione
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "routing_key,channel,node",
    [
        ("msh.EU_868.2.e.ALPHA.!bbad0ac8", "ALPHA", "!bbad0ac8"),
        ("opentakserver.2.e.LongFast.!12345678", "LongFast", "!12345678"),
        ("msh.EU_868.2.e.BRAVO", "BRAVO", None),
        ("qualcosa.di.altro", None, None),
    ],
)
def test_parsing_del_topic_mqtt(routing_key, channel, node):
    assert mesh.parse_mqtt_topic(routing_key) == (channel, node)


def test_canale_imparato_da_mqtt_riusato_per_il_relay(env):
    """Lo stesso node arriva via MQTT (dove il canale c'è) e via relay ATAK
    (dove non c'è): la seconda volta il canale si conosce lo stesso."""
    env["mappings"] = [Mapping(group_id=2, channel_name="BRAVO")]
    mesh.REGISTRY.node_channels["!bbad0ac8"] = {"name": "BRAVO", "index": 1}

    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(relay_cot(uid="!bbad0ac8"), "ANDROID-abc"))

    trace = mesh.REGISTRY.tags["!bbad0ac8"].last_routing
    assert trace["channel_name"] == "BRAVO"
    assert trace["channel_source"] == "mqtt_correlation"
    assert channel.published[0]["routing_key"] == "BRAVO.OUT"


# ----------------------------------------------------------------------
# Registry: limiti e log
# ----------------------------------------------------------------------

def test_il_log_eventi_e_limitato(env):
    for i in range(mesh.MAX_EVENTS + 50):
        mesh.REGISTRY.log("info", f"evento {i}")
    assert len(mesh.REGISTRY.events) == mesh.MAX_EVENTS


def test_log_incrementale_per_il_polling(env):
    mesh.REGISTRY.log("info", "uno")
    mark = mesh.REGISTRY.seq
    mesh.REGISTRY.log("info", "due")
    nuovi = mesh.REGISTRY.events_since(mark)
    assert [e["text"] for e in nuovi] == ["due"]


def test_i_pacchetti_per_tag_sono_limitati(env):
    channel = FakeChannel()
    for i in range(mesh.MAX_PACKETS_PER_TAG + 10):
        mesh.handle_cot(channel, firehose(relay_cot(time=f"2026-09-22T20:34:{i % 60:02d}Z"), "ANDROID-abc"))
    assert len(mesh.REGISTRY.tags["ALPHA-1"].packets) == mesh.MAX_PACKETS_PER_TAG


def test_snapshot_serializzabile_in_json(env):
    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(relay_cot(), "ANDROID-abc"))
    json.dumps(mesh.REGISTRY.snapshot(60, 300, 120))


def test_cento_tag_nel_registry(env):
    """Requisito di §20: 100 tag simultanei senza degrado del monitor."""
    channel = FakeChannel()
    for i in range(100):
        mesh.handle_cot(channel, firehose(relay_cot(uid=f"TAG-{i}", callsign=f"TAG-{i}"), "ANDROID-abc"))
    snapshot = mesh.REGISTRY.snapshot(60, 300, 120)
    assert len(snapshot) == 100
    assert all(row["status"] == "live" for row in snapshot)


def test_cache_delle_mappature_invalidata_applica_subito(env):
    """La cache TTL evita una query per pacchetto; le modifiche dalla UI
    chiamano invalidate_cache() e devono valere dal pacchetto successivo."""
    env["overrides"] = {"ALPHA-1": {"manual_channel_name": "ALPHA"}}
    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(relay_cot(time="2026-09-22T20:34:12Z"), "ANDROID-abc"))
    assert channel.published == []          # nessuna mappatura ancora

    env["mappings"] = [Mapping(group_id=1, channel_name="ALPHA")]
    mesh.invalidate_cache()
    mesh.handle_cot(channel, firehose(relay_cot(time="2026-09-22T20:34:22Z"), "ANDROID-abc"))
    assert channel.published[0]["routing_key"] == "ALPHA.OUT"


def test_forget_rimuove_tag_e_alias(env):
    channel = FakeChannel()
    mesh.handle_cot(channel, firehose(native_cot(uid="bbad0ac8"), None))
    mesh.REGISTRY.forget("!bbad0ac8")
    assert mesh.REGISTRY.tags == {}
    assert mesh.REGISTRY.aliases == {}
