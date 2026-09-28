"""Chat Meshtastic: crittografia e pacchetti, senza DB né RabbitMQ.

Il pannello scrive sulla mesh e legge messaggi cifrati: se l'hash del canale o
il nonce AES sono sbagliati le radio scartano i nostri messaggi e noi non
leggiamo i loro, senza nessun errore visibile. Per questo i test fissano i
valori del firmware (hash di LongFast con la chiave di default = 8).
"""

import base64

import pytest

from ots_milsim_companion_plugin import chat

pytest.importorskip("cryptography", reason="libreria cryptography assente")
pytest.importorskip("meshtastic", reason="libreria meshtastic assente")


# ----------------------------------------------------------------------
# PSK
# ----------------------------------------------------------------------

def test_psk_aq_e_la_chiave_di_default():
    assert chat.expand_psk("AQ==") == chat.DEFAULT_KEY


def test_psk_di_un_byte_incrementa_l_ultimo_byte_della_chiave_di_default():
    key = chat.expand_psk(base64.b64encode(b"\x02").decode())
    assert key[:-1] == chat.DEFAULT_KEY[:-1]
    assert key[-1] == (chat.DEFAULT_KEY[-1] + 1) & 0xFF


@pytest.mark.parametrize("psk", ["", "AA=="])
def test_psk_vuota_o_zero_vuol_dire_nessuna_cifratura(psk):
    assert chat.expand_psk(psk) == b""


def test_psk_di_16_e_32_byte_si_usano_cosi_come_sono():
    for size in (16, 32):
        raw = bytes(range(size))
        assert chat.expand_psk(base64.b64encode(raw).decode()) == raw


@pytest.mark.parametrize("psk", ["non-base64!!", base64.b64encode(b"12345").decode(), base64.b64encode(b"\x0b").decode()])
def test_psk_non_valide_danno_un_errore_leggibile(psk):
    with pytest.raises(chat.ChatError):
        chat.expand_psk(psk)


# ----------------------------------------------------------------------
# Valori del firmware
# ----------------------------------------------------------------------

def test_hash_di_longfast_con_chiave_di_default_e_8():
    """Valore noto: sul feed MQTT pubblico i pacchetti LongFast hanno channel = 8."""
    assert chat.channel_hash("LongFast", chat.DEFAULT_KEY) == 8


def test_aes_ctr_e_simmetrico_e_dipende_da_id_e_mittente():
    data = b"ciao dalla mesh"
    enc = chat.crypt(chat.DEFAULT_KEY, 1234, 0xBBAD0AA4, data)
    assert enc != data
    assert chat.crypt(chat.DEFAULT_KEY, 1234, 0xBBAD0AA4, enc) == data
    assert chat.crypt(chat.DEFAULT_KEY, 1235, 0xBBAD0AA4, data) != enc


def test_node_id_e_numero_si_convertono_nei_due_sensi():
    assert chat.node_num("!4d494c53") == 0x4D494C53
    assert chat.node_id(0x4D494C53) == "!4d494c53"
    with pytest.raises(chat.ChatError):
        chat.node_num("!zzzz")
    with pytest.raises(chat.ChatError):
        chat.node_num("!ffffffff")  # è l'indirizzo di broadcast


def test_topic_radice_dalla_routing_key():
    assert chat.topic_root("msh.EU_868.2.e.ALPHA.!bbad0aa4") == "msh.EU_868"
    assert chat.topic_root("msh.2.e.ALPHA.!bbad0aa4") == "msh"
    assert chat.topic_root("opentakserver.2.e.ALPHA.outgoing") == "opentakserver"
    assert chat.topic_root("qualcosa.senza.marcatore") is None


# ----------------------------------------------------------------------
# Pacchetti completi
# ----------------------------------------------------------------------

KEY = chat.expand_psk(base64.b64encode(bytes(range(16))).decode())
SENDER = 0x4D494C53


def _open(body, key):
    return chat.open_envelope(body, lambda channel: key)


def test_messaggio_cifrato_si_rilegge_con_la_stessa_psk():
    from meshtastic import portnums_pb2

    body, packet_id = chat.build_envelope("ALPHA", KEY, SENDER, chat.text_data("contatto a nord"))
    info = _open(body, KEY)
    assert info["encrypted"] is True
    assert info["channel"] == "ALPHA"
    assert info["gateway"] == "!4d494c53"
    assert info["from"] == SENDER and info["packet_id"] == packet_id
    assert info["data"].portnum == portnums_pb2.PortNum.TEXT_MESSAGE_APP
    assert info["data"].payload.decode() == "contatto a nord"


def test_hash_del_canale_nel_pacchetto_e_quello_del_firmware():
    from meshtastic import mqtt_pb2

    body, _ = chat.build_envelope("ALPHA", KEY, SENDER, chat.text_data("x"))
    envelope = mqtt_pb2.ServiceEnvelope()
    envelope.ParseFromString(body)
    assert envelope.packet.channel == chat.channel_hash("ALPHA", KEY)
    assert not envelope.packet.HasField("decoded")  # sui canali cifrati il firmware scarta il chiaro


def test_con_la_psk_sbagliata_non_si_legge_niente():
    body, _ = chat.build_envelope("ALPHA", KEY, SENDER, chat.text_data("segreto"))
    info = _open(body, chat.DEFAULT_KEY)
    assert info["data"] is None or info["data"].payload != b"segreto"


def test_senza_psk_restano_solo_i_metadati():
    body, _ = chat.build_envelope("ALPHA", KEY, SENDER, chat.text_data("segreto"))
    info = _open(body, None)
    assert info["encrypted"] is True
    assert info["data"] is None
    assert info["decrypt_failed"] is False


def test_canale_senza_cifratura_viaggia_in_chiaro():
    body, _ = chat.build_envelope("OPEN", b"", SENDER, chat.text_data("in chiaro"))
    info = _open(body, None)
    assert info["encrypted"] is False
    assert info["data"].payload.decode() == "in chiaro"


def test_nodeinfo_del_nodo_virtuale():
    from meshtastic import mesh_pb2, portnums_pb2

    body, _ = chat.build_envelope("ALPHA", KEY, SENDER, chat.nodeinfo_data(SENDER, "MilSim HQ", "HQ"))
    data = _open(body, KEY)["data"]
    assert data.portnum == portnums_pb2.PortNum.NODEINFO_APP
    user = mesh_pb2.User()
    user.ParseFromString(data.payload)
    assert (user.id, user.long_name, user.short_name) == ("!4d494c53", "MilSim HQ", "HQ")
