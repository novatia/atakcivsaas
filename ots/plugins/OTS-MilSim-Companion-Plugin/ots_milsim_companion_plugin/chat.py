# Chat Meshtastic (pannello nella tab Meshtastic).
#
# Ricezione: l'observer MQTT di mesh.py passa qui ogni pacchetto grezzo. I
# pacchetti dei gateway sono quasi sempre CIFRATI (uplink cifrato è il default
# del firmware): li decifriamo con la PSK del canale, come fa il
# meshtastic_controller di OpenTAKServer — AES-CTR, nonce = id pacchetto (u64 LE)
# + nodo mittente (u32 LE) + 4 byte a zero. I TEXT_MESSAGE_APP finiscono in
# `msh_chat_messages`; gli altri payload decifrati tornano a mesh.py, che così
# vede posizione/anagrafica anche dei pacchetti cifrati.
#
# Invio: si pubblica un ServiceEnvelope CIFRATO su amq.topic con la stessa
# routing key che usano i gateway (`<root>.2.e.<canale>.<!nodo>`, il plugin MQTT
# di RabbitMQ traduce `.` in `/`). I gateway con downlink attivo sul canale lo
# trasmettono in radio; il firmware scarta i pacchetti in chiaro sui canali
# cifrati, per questo si cifra. Il mittente è UN nodo virtuale del server
# (OTS_MILSIM_MESH_CHAT_NODE_ID): chi ha scritto resta nello storico.
# OpenTAKServer vede passare lo stesso pacchetto e, se il canale è fra i suoi,
# lo gira anche agli ATAK come GeoChat.
#
# PSK: prima quella della mappatura del canale nella tab «Canali Meshtastic»,
# altrimenti quella dello stesso canale nella tabella meshtastic_channels di OTS.

import base64
import logging
import random
import struct
import threading
import time
from datetime import datetime, timedelta

logger = logging.getLogger("OpenTAKServer")

# Chiave di default del firmware: la PSK «AQ==» (1 byte = 1) significa proprio questa
DEFAULT_KEY = base64.b64decode("1PG7OiApB1nwvP+rz05pAQ==")
BROADCAST = 0xFFFFFFFF
# Il payload di un MeshPacket sta in ~233 byte; 200 lascia margine a cifratura e header
MAX_TEXT_BYTES = 200
# Ogni quanto ripetere il NODEINFO del nodo virtuale, perché le radio mostrino
# «MilSim HQ» invece di un id anonimo
NODEINFO_EVERY = 3 * 3600

DIRECTION_RX = "rx"
DIRECTION_TX = "tx"
DIRECTION_ATAK = "atak"


class ChatError(Exception):
    """Errore da mostrare così com'è all'utente del pannello."""


# ----------------------------------------------------------------------
# Crittografia (funzioni pure)
# ----------------------------------------------------------------------


def expand_psk(psk_b64: str) -> bytes:
    """Chiave AES dalla PSK in base64 come la scrive l'app Meshtastic.

    b"" = canale senza cifratura. 1 byte: 0 = senza cifratura, 1..10 = chiave
    di default con l'ultimo byte incrementato di (n-1). 16 o 32 byte: AES-128/256.
    Solleva ChatError se la PSK non è valida.
    """
    try:
        raw = base64.b64decode((psk_b64 or "").strip(), validate=True)
    except BaseException:
        raise ChatError("PSK non valida: deve essere in base64 (come la mostra l'app Meshtastic)")
    if len(raw) == 0:
        return b""
    if len(raw) == 1:
        index = raw[0]
        if index == 0:
            return b""
        if index > 10:
            raise ChatError("PSK di 1 byte non valida: vale 0 (nessuna cifratura) o 1-10 (chiave di default)")
        key = bytearray(DEFAULT_KEY)
        key[-1] = (key[-1] + index - 1) & 0xFF
        return bytes(key)
    if len(raw) in (16, 32):
        return raw
    raise ChatError(f"PSK di {len(raw)} byte non valida: servono 1, 16 o 32 byte")


def _xor(data: bytes) -> int:
    value = 0
    for b in data:
        value ^= b
    return value


def channel_hash(name: str, key: bytes) -> int:
    """Hash del canale che il firmware mette in MeshPacket.channel: xor dei byte
    del nome xor xor dei byte della chiave."""
    return _xor((name or "").encode("utf-8")) ^ _xor(key)


def crypt(key: bytes, packet_id: int, from_node: int, data: bytes) -> bytes:
    """AES-CTR del firmware Meshtastic (stessa operazione per cifrare e decifrare)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    nonce = struct.pack("<QI", packet_id & 0xFFFFFFFFFFFFFFFF, from_node & 0xFFFFFFFF) + b"\x00" * 4
    cipher = Cipher(algorithms.AES(key), modes.CTR(nonce))
    op = cipher.encryptor()
    return op.update(data) + op.finalize()


def node_num(node_id: str) -> int:
    """`!4d494c53` -> 0x4d494c53."""
    text = (node_id or "").strip().lstrip("!")
    try:
        value = int(text, 16)
    except ValueError:
        raise ChatError(f"Node id non valido: {node_id!r} (formato !xxxxxxxx)")
    if not 0 < value < BROADCAST:
        raise ChatError(f"Node id fuori intervallo: {node_id!r}")
    return value


def node_id(number: int) -> str:
    return f"!{number:08x}"


def topic_root(routing_key: str) -> str | None:
    """Prefisso del topic prima di `2.e`: `msh.EU_868.2.e.ALPHA.!x` -> `msh.EU_868`."""
    parts = [p for p in (routing_key or "").split(".") if p]
    for i in range(len(parts) - 1):
        if parts[i] == "2" and parts[i + 1] in ("e", "c"):
            return ".".join(parts[:i]) or None
    return None


def build_envelope(channel: str, key: bytes, from_node: int, data_bytes: bytes,
                   packet_id: int | None = None, hop_limit: int = 3, to: int = BROADCAST) -> tuple[bytes, int]:
    """ServiceEnvelope pronto da pubblicare. Ritorna (bytes, id pacchetto)."""
    from meshtastic import mesh_pb2, mqtt_pb2

    packet_id = packet_id or random.randint(1, 0xFFFFFFFF)
    packet = mesh_pb2.MeshPacket()
    setattr(packet, "from", from_node)
    packet.to = to
    packet.id = packet_id
    packet.hop_limit = hop_limit
    packet.hop_start = hop_limit
    packet.channel = channel_hash(channel, key)
    if key:
        packet.encrypted = crypt(key, packet_id, from_node, data_bytes)
    else:
        packet.decoded.ParseFromString(data_bytes)
    envelope = mqtt_pb2.ServiceEnvelope()
    envelope.packet.CopyFrom(packet)
    envelope.channel_id = channel
    envelope.gateway_id = node_id(from_node)
    return envelope.SerializeToString(), packet_id


def text_data(text: str) -> bytes:
    from meshtastic import mesh_pb2, portnums_pb2

    data = mesh_pb2.Data()
    data.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
    data.payload = text.encode("utf-8")
    return data.SerializeToString()


def nodeinfo_data(from_node: int, long_name: str, short_name: str) -> bytes:
    from meshtastic import mesh_pb2, portnums_pb2

    user = mesh_pb2.User()
    user.id = node_id(from_node)
    user.long_name = long_name[:39]
    user.short_name = short_name[:4]
    data = mesh_pb2.Data()
    data.portnum = portnums_pb2.PortNum.NODEINFO_APP
    data.payload = user.SerializeToString()
    return data.SerializeToString()


def open_envelope(body: bytes, key_for_channel) -> dict | None:
    """Apre un ServiceEnvelope. `key_for_channel(nome) -> bytes | None` dà la
    chiave (None = PSK sconosciuta). Ritorna un dict con i metadati, `data`
    (mesh_pb2.Data o None) e `decrypt_failed` se c'era una chiave ma non torna."""
    from meshtastic import mesh_pb2, mqtt_pb2

    envelope = mqtt_pb2.ServiceEnvelope()
    try:
        envelope.ParseFromString(body)
    except BaseException:
        return None
    packet = envelope.packet
    info = {
        "channel": envelope.channel_id or None,
        "gateway": envelope.gateway_id or None,
        "from": getattr(packet, "from"),
        "to": packet.to,
        "packet_id": packet.id,
        "rssi": packet.rx_rssi or None,
        "snr": round(packet.rx_snr, 2) if packet.rx_snr else None,
        "hop_count": (packet.hop_start - packet.hop_limit) if packet.hop_start else None,
        "data": None,
        "decrypt_failed": False,
        "encrypted": False,
    }
    if packet.HasField("decoded"):
        info["data"] = packet.decoded
        return info
    if not packet.encrypted:
        return info
    info["encrypted"] = True
    key = key_for_channel(info["channel"]) if info["channel"] else None
    if not key:
        return info
    data = mesh_pb2.Data()
    try:
        data.ParseFromString(crypt(key, packet.id, info["from"], packet.encrypted))
        # Con la chiave sbagliata il protobuf spesso si «decodifica» lo stesso in
        # spazzatura: un portnum 0 (UNKNOWN_APP) è il segnale più affidabile
        if not data.portnum:
            raise ValueError("portnum nullo")
        info["data"] = data
    except BaseException:
        info["decrypt_failed"] = True
    return info


# ----------------------------------------------------------------------
# Stato in memoria (solo processo web)
# ----------------------------------------------------------------------

_lock = threading.Lock()
# canale (minuscolo) -> prefisso del topic imparato dai gateway, es. «msh.EU_868»
ROOTS: dict[str, str] = {}
# canale (minuscolo) -> nome come arriva dal feed, per mostrarlo nel pannello
SEEN_NAMES: dict[str, str] = {}
# canale (minuscolo) -> {"ok": n, "failed": n, "encrypted_no_key": n}
STATS: dict[str, dict] = {}
# canale (minuscolo) -> ultimo NODEINFO inviato (epoch)
_nodeinfo_sent: dict[str, float] = {}
_last_prune = 0.0


def _stat(channel: str, field: str) -> None:
    with _lock:
        entry = STATS.setdefault((channel or "").lower(), {"ok": 0, "failed": 0, "encrypted_no_key": 0})
        entry[field] += 1


def learn_root(routing_key: str, channel: str | None) -> None:
    root = topic_root(routing_key)
    if root and channel and not routing_key.endswith("outgoing"):
        ROOTS[channel.lower()] = root
        SEEN_NAMES[channel.lower()] = channel


# ----------------------------------------------------------------------
# PSK
# ----------------------------------------------------------------------


def resolve_psk(channel: str) -> tuple[str | None, str | None]:
    """(psk base64, sorgente) per il canale: «plugin», «ots» o (None, None)."""
    from opentakserver.extensions import db

    from .models import MeshChannelMap

    name = (channel or "").strip().lower()
    if not name:
        return None, None
    for row in db.session.query(MeshChannelMap).all():
        if (row.channel_name or "").lower() == name and row.psk:
            return row.psk, "plugin"
    try:
        from opentakserver.models.Meshtastic import MeshtasticChannel

        for row in db.session.query(MeshtasticChannel).all():
            if (row.name or "").lower() == name and row.psk:
                return row.psk, "ots"
    except BaseException:
        db.session.rollback()
    return None, None


_key_cache: dict[str, tuple[float, bytes | None]] = {}


def key_for(channel: str) -> bytes | None:
    """Chiave AES del canale, con cache di 10 s (l'observer la chiede a ogni pacchetto)."""
    name = (channel or "").lower()
    cached = _key_cache.get(name)
    if cached and time.time() - cached[0] < 10:
        return cached[1]
    psk, _ = resolve_psk(channel)
    key = None
    if psk:
        try:
            key = expand_psk(psk) or None
        except ChatError:
            key = None
    _key_cache[name] = (time.time(), key)
    return key


def invalidate_keys() -> None:
    _key_cache.clear()


# ----------------------------------------------------------------------
# Ricezione
# ----------------------------------------------------------------------


def handle_mqtt(routing_key: str, body: bytes, config) -> object | None:
    """Chiamata dall'observer MQTT per ogni pacchetto. Salva i messaggi di testo
    e ritorna il mesh_pb2.Data decifrato (o None) perché mesh.py lo interpreti."""
    from meshtastic import portnums_pb2

    outgoing = routing_key.endswith("outgoing")
    info = open_envelope(body, key_for)
    if not info:
        return None
    channel = info["channel"]
    if not outgoing:
        learn_root(routing_key, channel)
    if info["encrypted"]:
        if info["data"] is not None:
            _stat(channel, "ok")
        elif info["decrypt_failed"]:
            _stat(channel, "failed")
        else:
            _stat(channel, "encrypted_no_key")

    data = info["data"]
    if data is None or data.portnum != portnums_pb2.PortNum.TEXT_MESSAGE_APP:
        return data

    own = (config.get("OTS_MILSIM_MESH_CHAT_NODE_ID") or "!4d494c53").lower()
    if info["gateway"] and info["gateway"].lower() == own:
        # La nostra stessa pubblicazione rivista dall'observer: già salvata all'invio
        return data
    store_message(
        channel=channel,
        packet_id=info["packet_id"],
        from_node=node_id(info["from"]) if info["from"] else None,
        to_node=node_id(info["to"]) if info["to"] and info["to"] != BROADCAST else None,
        text=data.payload.decode("utf-8", "replace"),
        direction=DIRECTION_ATAK if outgoing else DIRECTION_RX,
        gateway=info["gateway"],
        rssi=info["rssi"],
        snr=info["snr"],
        hop_count=info["hop_count"],
        config=config,
    )
    return data


def store_message(channel, packet_id, from_node, to_node, text, direction, gateway=None,
                  rssi=None, snr=None, hop_count=None, author=None, config=None) -> dict:
    """Inserisce o, se lo stesso pacchetto arriva da un altro gateway, aggiorna.
    Un nostro messaggio rivisto da un gateway diventa «sentito dalla mesh»."""
    from opentakserver.extensions import db

    from .models import MeshChatMessage

    row = None
    if packet_id and from_node:
        row = db.session.query(MeshChatMessage).filter_by(from_node=from_node, packet_id=packet_id).first()
    if row:
        row.heard_count = (row.heard_count or 0) + 1
        if row.direction == DIRECTION_TX:
            row.status = "heard"
            row.rx_gateway = row.rx_gateway or gateway
        if rssi is not None and (row.rssi is None or rssi > row.rssi):
            row.rssi, row.snr = rssi, snr
        db.session.commit()
        return row.serialize()

    row = MeshChatMessage(
        channel_name=channel,
        packet_id=packet_id,
        from_node=from_node,
        to_node=to_node,
        text=text,
        direction=direction,
        author=author,
        rx_gateway=gateway,
        rssi=rssi,
        snr=snr,
        hop_count=hop_count,
        heard_count=0 if direction == DIRECTION_TX else 1,
        status="sent" if direction == DIRECTION_TX else None,
        created_at=datetime.utcnow(),
    )
    db.session.add(row)
    db.session.commit()
    _maybe_prune(config)
    return row.serialize()


def _maybe_prune(config) -> None:
    global _last_prune
    if time.time() - _last_prune < 3600:
        return
    _last_prune = time.time()
    days = int((config or {}).get("OTS_MILSIM_MESH_CHAT_RETENTION_DAYS", 180) or 0)
    if days <= 0:
        return
    from opentakserver.extensions import db

    from .models import MeshChatMessage

    try:
        cutoff = datetime.utcnow() - timedelta(days=days)
        deleted = db.session.query(MeshChatMessage).filter(MeshChatMessage.created_at < cutoff).delete()
        db.session.commit()
        if deleted:
            logger.info(f"MilSim chat: rimossi {deleted} messaggi più vecchi di {days} giorni")
    except BaseException:
        db.session.rollback()


# ----------------------------------------------------------------------
# Invio
# ----------------------------------------------------------------------


def _publish(config, routing_key: str, body: bytes) -> None:
    import pika

    credentials = pika.PlainCredentials(config.get("OTS_RABBITMQ_USERNAME"), config.get("OTS_RABBITMQ_PASSWORD"))
    connection = pika.BlockingConnection(
        pika.ConnectionParameters(host=config.get("OTS_RABBITMQ_SERVER_ADDRESS"), credentials=credentials,
                                  socket_timeout=5, blocked_connection_timeout=5)
    )
    try:
        connection.channel().basic_publish(exchange="amq.topic", routing_key=routing_key, body=body)
    finally:
        connection.close()


def channel_root(config, channel: str) -> str | None:
    configured = (config.get("OTS_MILSIM_MESH_CHAT_ROOT_TOPIC") or "").strip().strip("/")
    if configured:
        return configured.replace("/", ".")
    return ROOTS.get((channel or "").lower())


def send_text(config, channel: str, text: str, author: str | None) -> dict:
    channel = (channel or "").strip()
    text = (text or "").strip()
    if not channel:
        raise ChatError("Scegli un canale")
    if not text:
        raise ChatError("Il messaggio è vuoto")
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ChatError(f"Messaggio troppo lungo: massimo {MAX_TEXT_BYTES} byte (le lettere accentate ne valgono 2)")

    psk, _ = resolve_psk(channel)
    if not psk:
        raise ChatError(f"Nessuna PSK per il canale {channel}: impostala nella tab «Canali Meshtastic»")
    key = expand_psk(psk)
    root = channel_root(config, channel)
    if not root:
        raise ChatError(
            f"Topic MQTT del canale {channel} sconosciuto: nessun gateway ha ancora pubblicato su questo canale "
            "da quando OpenTAKServer è partito. Aspetta un pacchetto dal canale oppure imposta il topic radice "
            "(es. msh/EU_868) nella tab «Canali Meshtastic»."
        )
    sender = node_num(config.get("OTS_MILSIM_MESH_CHAT_NODE_ID") or "!4d494c53")
    hop_limit = int(config.get("OTS_MILSIM_MESH_CHAT_HOP_LIMIT", 3) or 3)
    routing_key = f"{root}.2.e.{channel}.{node_id(sender)}"

    now = time.time()
    if now - _nodeinfo_sent.get(channel.lower(), 0) > NODEINFO_EVERY:
        info_body, _ = build_envelope(
            channel, key, sender,
            nodeinfo_data(sender, config.get("OTS_MILSIM_MESH_CHAT_LONG_NAME") or "MilSim HQ",
                          config.get("OTS_MILSIM_MESH_CHAT_SHORT_NAME") or "HQ"),
            hop_limit=hop_limit,
        )
        _publish(config, routing_key, info_body)
        _nodeinfo_sent[channel.lower()] = now

    body, packet_id = build_envelope(channel, key, sender, text_data(text), hop_limit=hop_limit)
    _publish(config, routing_key, body)
    logger.info(f"MilSim chat: messaggio di {author or '?'} inviato su {channel} ({len(text)} caratteri)")
    return store_message(
        channel=channel, packet_id=packet_id, from_node=node_id(sender), to_node=None, text=text,
        direction=DIRECTION_TX, author=author, config=config,
    )


# ----------------------------------------------------------------------
# Lettura per la UI
# ----------------------------------------------------------------------


def channels(config) -> list[dict]:
    """Canali proponibili nel pannello: mappati, di OTS, visti sul feed o con messaggi."""
    from opentakserver.extensions import db
    from sqlalchemy import func

    from .models import MeshChannelMap, MeshChatMessage

    names: dict[str, str] = {}

    def add(name):
        if name and name.lower() not in names:
            names[name.lower()] = name

    for row in db.session.query(MeshChannelMap).all():
        add(row.channel_name)
    try:
        from opentakserver.models.Meshtastic import MeshtasticChannel

        for row in db.session.query(MeshtasticChannel).all():
            add(row.name)
    except BaseException:
        db.session.rollback()
    for name in list(SEEN_NAMES.values()):
        add(name)
    last = dict(
        db.session.query(MeshChatMessage.channel_name, func.max(MeshChatMessage.created_at))
        .group_by(MeshChatMessage.channel_name).all()
    )
    for name in last:
        add(name)

    result = []
    for name in sorted(names.values(), key=str.lower):
        psk, source = resolve_psk(name)
        problem = None
        if not psk:
            problem = "PSK non configurata"
        else:
            try:
                expand_psk(psk)
            except ChatError as e:
                problem = str(e)
        if not problem and not channel_root(config, name):
            problem = "topic MQTT non ancora visto"
        stats = STATS.get(name.lower(), {})
        last_at = next((v for k, v in last.items() if (k or "").lower() == name.lower()), None)
        result.append({
            "name": name,
            "psk_source": source,
            "root": channel_root(config, name),
            "can_send": problem is None,
            "problem": problem,
            "decrypt_ok": stats.get("ok", 0),
            "decrypt_failed": stats.get("failed", 0),
            "encrypted_no_key": stats.get("encrypted_no_key", 0),
            "last_message_at": last_at.isoformat() + "Z" if last_at else None,
        })
    return result


def messages(channel: str, after_id: int = 0, limit: int = 200) -> list[dict]:
    from opentakserver.extensions import db

    from .models import MeshChatMessage, MeshTag

    query = db.session.query(MeshChatMessage).filter(
        db.func.lower(MeshChatMessage.channel_name) == (channel or "").lower()
    )
    if after_id:
        rows = query.filter(MeshChatMessage.id > after_id).order_by(MeshChatMessage.id).limit(limit).all()
    else:
        rows = list(reversed(query.order_by(MeshChatMessage.id.desc()).limit(limit).all()))
    nodes = {r.from_node for r in rows if r.from_node}
    names = {}
    if nodes:
        for tag in db.session.query(MeshTag).filter(MeshTag.node_id.in_(nodes)).all():
            names[tag.node_id] = tag.long_name or tag.short_name or tag.callsign
    out = []
    for r in rows:
        item = r.serialize()
        item["sender_name"] = names.get(r.from_node)
        out.append(item)
    return out
