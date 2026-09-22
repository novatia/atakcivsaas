# Integrazione Meshtastic / TAK tracker.
#
# Due strade di ingresso, trattate separatamente perché portano informazioni
# molto diverse (analisi completa in docs/meshtastic-architettura.md):
#
#   Path A — relay ATAK:  tag -> LoRa -> nodo su Android -> plugin Meshtastic
#                         di ATAK -> "Relay to Server" -> CoT -> eud_handler.
#                         Nel CoT c'è solo `<__meshtastic/>` VUOTO: niente
#                         canale, niente node id, niente RSSI/SNR.
#   Path B — MQTT diretto: tag -> LoRa -> gateway -> MQTT -> RabbitMQ.
#                         Qui il canale c'è (sta nella routing key) e con il
#                         protobuf si leggono node id, RSSI, SNR, hop, batteria.
#
# Il plugin OSSERVA entrambe le strade dal firehose (fanout: copia di ogni CoT,
# è l'hook che OTS dichiara per i plugin) e dal feed MQTT grezzo su `amq.topic`.
# Non genera mai un CoT al posto di OTS: l'unica pubblicazione è l'eventuale
# consegna aggiuntiva al gruppo mappato, sull'exchange `groups` con la stessa
# routing key `<gruppo>.OUT` che usa il cot_parser — stesso meccanismo di
# autorizzazione, nessuna scorciatoia.

import json
import re
import threading
import time
import traceback
import xml.etree.ElementTree as ET
from collections import OrderedDict, deque
from datetime import datetime, timedelta, timezone

import pika
from flask import current_app as app

from opentakserver.extensions import db, logger

# Sorgenti (transport) di una ricezione
SOURCE_ATAK_RELAY = "atak_relay"
SOURCE_MQTT = "mqtt"
SOURCE_OTS_MESHTASTIC = "ots_meshtastic"

SOURCE_LABELS = {
    SOURCE_ATAK_RELAY: "ATAK Relay",
    SOURCE_MQTT: "MQTT",
    SOURCE_OTS_MESHTASTIC: "OTS Meshtastic",
}

# Politiche di fallback quando il canale non è determinabile
FALLBACK_POLICIES = ("source_eud_group", "default_group", "meshtastic_group", "ignore")

# Esiti dell'instradamento
RESULT_ROUTED = "routed"
RESULT_ROUTED_FALLBACK = "routed_fallback"
RESULT_NATIVE_ONLY = "native_only"
RESULT_IGNORED = "ignored"
RESULT_ERROR = "error"
RESULT_OBSERVED = "observed"

# Tetti della memoria: con 100 tag il registry costa ~100 * (stato + 25 pacchetti)
MAX_TAGS = 500
MAX_PACKETS_PER_TAG = 25
MAX_EVENTS = 800
# Il CoT grezzo tenuto per il "View Raw CoT" (già sanificato)
MAX_RAW_COT_CHARS = 8000

# Attributi/elementi il cui contenuto non deve MAI arrivare alla UI di debug.
# Vale per il CoT grezzo e per ogni metadato mostrato.
SECRET_PATTERN = re.compile(
    r"psk|password|passwd|secret|token|apikey|api_key|privatekey|private_key"
    r"|credential|cookie|authorization|auth_|session|certificate|\bcert\b|keyfile",
    re.IGNORECASE,
)
REDACTED = "[REDACTED]"

# Meshtastic ha 8 canali: gli indici validi vanno da 0 a 7. L'hash del canale,
# che è quello che viaggia in `MeshPacket.channel` sul feed MQTT (vedi
# `decode_service_envelope`), è un byte qualsiasi: il range serve a non
# scambiare un hash per un indice.
MAX_CHANNEL_INDEX = 7


def is_channel_index(value) -> bool:
    """Vero solo per un indice di canale Meshtastic plausibile (0-7)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return 0 <= value <= MAX_CHANNEL_INDEX


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _cfg(key: str, default=None):
    try:
        value = app.config.get(key)
    except BaseException:
        return default
    return default if value is None else value


# ----------------------------------------------------------------------
# Sanificazione
# ----------------------------------------------------------------------


def sanitize_element(element: ET.Element) -> ET.Element:
    """Copia dell'elemento con ogni attributo/testo sospetto oscurato.

    Non prova a capire se il valore è davvero un segreto: se il NOME assomiglia
    a un segreto il valore sparisce. Meglio oscurare un campo innocuo che far
    trapelare una PSK di canale in una schermata di debug.
    """
    clone = ET.Element(element.tag)
    for name, value in element.attrib.items():
        clone.set(name, REDACTED if SECRET_PATTERN.search(name) else value)
    if SECRET_PATTERN.search(element.tag):
        clone.text = REDACTED if (element.text or "").strip() else element.text
    else:
        clone.text = element.text
    clone.tail = element.tail
    for child in element:
        clone.append(sanitize_element(child))
    return clone


def sanitize_cot_xml(xml: str) -> str:
    """CoT pronto per la UI: attributi segreti oscurati, XML indentato, troncato.

    Se il parse fallisce non si mostra il testo originale (potrebbe contenere
    di tutto): si restituisce un segnaposto.
    """
    if not xml:
        return ""
    try:
        root = ET.fromstring(xml)
    except BaseException:
        return "[CoT non analizzabile: non mostrato]"
    clean = sanitize_element(root)
    try:
        ET.indent(clean, space="  ")
    except BaseException:
        pass
    text = ET.tostring(clean, encoding="unicode")
    if len(text) > MAX_RAW_COT_CHARS:
        text = text[:MAX_RAW_COT_CHARS] + "\n<!-- troncato -->"
    return text


def sanitize_value(name: str, value):
    return REDACTED if SECRET_PATTERN.search(str(name)) else value


# ----------------------------------------------------------------------
# Riconoscimento di un CoT «Meshtastic»
# ----------------------------------------------------------------------


def detect(xml: str, sender_uid: str | None) -> dict | None:
    """Estrae il descrittore di un tag da un CoT, o None se non è Meshtastic.

    Discriminanti, in ordine di affidabilità:
      1. `<detail><__meshtastic/>` — lo mette il plugin Meshtastic di ATAK su
         OGNI evento che inietta (verificato sul sorgente: 9 occorrenze, sempre
         elemento vuoto). È il marcatore del Path A.
      2. `<takv platform="Meshtastic">` / `<contact endpoint="MQTT">` — li mette
         il meshtastic_controller di OTS quando genera CoT dal feed MQTT.

    Nessuna euristica sul callsign: un tag non si riconosce dal nome.
    """
    try:
        event = ET.fromstring(xml)
    except BaseException:
        return None
    if event.tag != "event":
        return None

    detail = event.find("detail")
    if detail is None:
        return None

    mesh_tag = detail.find("__meshtastic")
    takv = detail.find("takv")
    contact = detail.find("contact")

    native = takv is not None and (takv.get("platform") == "Meshtastic" or takv.get("os") == "Meshtastic")
    if contact is not None and contact.get("endpoint") == "MQTT":
        native = True

    if mesh_tag is None and not native:
        return None

    uid = event.get("uid") or ""
    if not uid:
        return None

    # Le chat non sono tag: non hanno posizione e il loro uid è per messaggio
    if uid.startswith("GeoChat.") or event.get("type") == "b-t-f":
        return None

    source = SOURCE_OTS_MESHTASTIC if (native and mesh_tag is None) else SOURCE_ATAK_RELAY

    descriptor = {
        "uid": uid,
        "cot_type": event.get("type"),
        "how": event.get("how"),
        "time": event.get("time"),
        "stale": event.get("stale"),
        "source": source,
        "source_eud": sender_uid,
        # Path A: il canale NON c'è. Si tiene traccia del fatto che gli
        # attributi di <__meshtastic> erano assenti, serve alla UI (§16).
        "channel_metadata_present": bool(mesh_tag is not None and mesh_tag.attrib),
        "node_id": None,
        "callsign": None,
        "long_name": None,
        "short_name": None,
        "role": None,
        "team": None,
        "battery": None,
        "latitude": None,
        "longitude": None,
        "altitude": None,
        "speed": None,
        "course": None,
        "gps": "none",
        "channel_index": None,
        "channel_name": None,
        # Hash del canale: dato diagnostico del feed MQTT, mai un indice.
        "channel_hash": None,
    }

    # Se un giorno il plugin ATAK aggiungesse gli attributi a <__meshtastic>
    # (proposta 4 del documento di analisi) li si legge senza altre modifiche.
    if mesh_tag is not None:
        for key in ("channel", "channel_index", "channelIndex"):
            if mesh_tag.get(key) is not None:
                try:
                    value = int(mesh_tag.get(key))
                except ValueError:
                    break
                # Questo È l'indice vero, dichiarato dal plugin ATAK, non
                # l'hash del protobuf: si accetta solo se sta in 0-7.
                if is_channel_index(value):
                    descriptor["channel_index"] = value
                break
        for key in ("channel_name", "channelName"):
            if mesh_tag.get(key):
                descriptor["channel_name"] = mesh_tag.get(key)
                break
        for key in ("node_id", "nodeId", "from"):
            if mesh_tag.get(key):
                descriptor["node_id"] = normalize_node_id(mesh_tag.get(key))
                break

    # L'uid del CoT È il node id quando arriva già in forma canonica `!xxxxxxxx`
    # (lo fa il meshtastic_controller di OTS quando l'EUD non è mappato). È un
    # confronto esatto, non un'euristica sul nome: `ALPHA-1` non passa.
    if descriptor["node_id"] is None and uid.startswith("!") and normalize_node_id(uid) == uid:
        descriptor["node_id"] = uid

    if takv is not None:
        if takv.get("meshtastic_id"):
            descriptor["node_id"] = normalize_node_id(takv.get("meshtastic_id"))
        descriptor["device"] = takv.get("device")
        descriptor["firmware"] = takv.get("version")

    if contact is not None:
        descriptor["callsign"] = contact.get("callsign")
        descriptor["long_name"] = contact.get("callsign")

    uid_el = detail.find("uid")
    if uid_el is not None and uid_el.get("Droid"):
        descriptor["long_name"] = uid_el.get("Droid")

    group_el = detail.find("__group")
    if group_el is not None:
        descriptor["role"] = group_el.get("role")
        descriptor["team"] = group_el.get("name")

    status = detail.find("status")
    if status is not None and status.get("battery"):
        try:
            descriptor["battery"] = int(float(status.get("battery")))
        except ValueError:
            pass

    track = detail.find("track")
    if track is not None:
        descriptor["speed"] = _float_or_none(track.get("speed"))
        descriptor["course"] = _float_or_none(track.get("course"))

    point = event.find("point")
    if point is not None:
        lat = _float_or_none(point.get("lat"))
        lon = _float_or_none(point.get("lon"))
        hae = _float_or_none(point.get("hae"))
        # 9999999.0 è il "non disponibile" del CoT; 0/0 è il segnaposto di OTS
        if lat is not None and lon is not None and abs(lat) <= 90 and abs(lon) <= 180:
            if not (lat == 0 and lon == 0):
                descriptor["latitude"] = lat
                descriptor["longitude"] = lon
                descriptor["gps"] = "fix"
        if hae is not None and hae < 9999999:
            descriptor["altitude"] = hae

    return descriptor


def _float_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_node_id(raw) -> str | None:
    """Node id Meshtastic nella forma canonica `!bbad0ac8`.

    In giro si trova come intero decimale (protobuf), esadecimale senza `!`
    (meshtastic_controller di OTS) o già con `!` (UI Meshtastic).
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.startswith("!"):
        text = text[1:]
    try:
        if re.fullmatch(r"[0-9a-fA-F]{1,8}", text):
            value = int(text, 16)
        else:
            value = int(text)
    except ValueError:
        return None
    return f"!{value:08x}"


# ----------------------------------------------------------------------
# Stato vivo di un tag
# ----------------------------------------------------------------------


class Tag:
    """Un tag Meshtastic, unico anche se ricevuto da più gateway.

    L'identità stabile è il node id quando lo si conosce, altrimenti l'uid del
    CoT. Ogni strada di ricezione (EUD relay, gateway MQTT) è registrata in
    `paths`: la UI mostra "ricevuto via ATAK-EUD-01, ATAK-EUD-02, MQTT-GW-01"
    pur trattandolo come un solo oggetto logico.
    """

    __slots__ = (
        "key", "node_id", "uid", "callsign", "long_name", "short_name", "role",
        "team", "device", "firmware", "channel_index", "channel_name",
        "channel_hash", "channel_source", "latitude", "longitude", "altitude",
        "speed", "course", "gps", "battery", "voltage", "rssi", "snr",
        "hop_count", "first_seen",
        "last_seen", "last_position", "paths", "packets", "rx_count",
        "duplicate_count", "last_routing", "last_error", "position_precision",
        "_last_cot_key", "_persist_due",
    )

    def __init__(self, key: str):
        now = _utcnow()
        self.key = key
        self.node_id = None
        self.uid = None
        self.callsign = None
        self.long_name = None
        self.short_name = None
        self.role = None
        self.team = None
        self.device = None
        self.firmware = None
        self.channel_index = None
        self.channel_name = None
        self.channel_hash = None
        self.channel_source = None
        self.latitude = None
        self.longitude = None
        self.altitude = None
        self.speed = None
        self.course = None
        self.gps = "none"
        self.battery = None
        self.voltage = None
        self.rssi = None
        self.snr = None
        self.hop_count = None
        self.position_precision = None
        self.first_seen = now
        self.last_seen = now
        self.last_position = None
        self.paths = OrderedDict()
        self.packets = deque(maxlen=MAX_PACKETS_PER_TAG)
        self.rx_count = 0
        self.duplicate_count = 0
        self.last_routing = None
        self.last_error = None
        self._last_cot_key = None
        self._persist_due = 0.0

    # -- aggiornamento ------------------------------------------------

    def apply(self, descriptor: dict) -> bool:
        """Applica una ricezione. Ritorna False se è un duplicato esatto.

        Duplicato = stesso uid e stesso `time` del CoT già visto (lo stesso
        pacchetto arrivato da due gateway). Un aggiornamento di posizione più
        recente NON è mai un duplicato: cambia il `time`.
        """
        now = _utcnow()
        cot_key = (descriptor.get("uid"), descriptor.get("time"))
        duplicate = cot_key == self._last_cot_key and cot_key[1] is not None
        self._last_cot_key = cot_key

        self.last_seen = now
        self.rx_count += 1
        if duplicate:
            self.duplicate_count += 1

        for field in ("node_id", "uid", "callsign", "long_name", "short_name",
                      "role", "team", "device", "firmware"):
            value = descriptor.get(field)
            if value:
                setattr(self, field, value)

        # L'hash non è un canale: si registra sempre, ma da solo non basta a
        # dire su che canale è il tag (non si risale dall'hash al nome).
        if descriptor.get("channel_hash") is not None:
            self.channel_hash = descriptor["channel_hash"]

        if descriptor.get("channel_name") or descriptor.get("channel_index") is not None:
            self.channel_name = descriptor.get("channel_name") or self.channel_name
            if descriptor.get("channel_index") is not None:
                self.channel_index = descriptor["channel_index"]
            self.channel_source = descriptor.get("source")

        for field in ("battery", "voltage", "rssi", "snr", "hop_count", "position_precision"):
            if descriptor.get(field) is not None:
                setattr(self, field, descriptor[field])

        # Posizione: mai modificata, solo copiata. Se Meshtastic ha ridotto la
        # precisione la coordinata arriva già arrotondata e resta tale.
        if descriptor.get("latitude") is not None:
            self.latitude = descriptor["latitude"]
            self.longitude = descriptor["longitude"]
            self.gps = "fix"
            self.last_position = now
        if descriptor.get("altitude") is not None:
            self.altitude = descriptor["altitude"]
        for field in ("speed", "course"):
            if descriptor.get(field) is not None:
                setattr(self, field, descriptor[field])

        path_key = descriptor.get("source_eud") or SOURCE_LABELS.get(
            descriptor.get("source"), descriptor.get("source") or "?"
        )
        path = self.paths.get(path_key)
        if not path:
            path = {"key": path_key, "transport": descriptor.get("source"), "count": 0, "first_seen": now}
            self.paths[path_key] = path
        path["count"] += 1
        path["last_seen"] = now
        path["transport"] = descriptor.get("source") or path["transport"]

        return not duplicate

    def status(self, live_seconds: int, recent_seconds: int) -> str:
        age = (_utcnow() - self.last_seen).total_seconds()
        if age < live_seconds:
            return "live"
        if age < recent_seconds:
            return "recent"
        return "stale"

    def gps_status(self, stale_seconds: int) -> str:
        """`none` se non è mai arrivata una posizione, `stale` se è vecchia."""
        if self.last_position is None or self.latitude is None:
            return "none"
        if (_utcnow() - self.last_position).total_seconds() >= stale_seconds:
            return "stale"
        return "fix"

    def serialize(self, live_seconds: int, recent_seconds: int, gps_stale_seconds: int) -> dict:
        now = _utcnow()
        return {
            "key": self.key,
            "node_id": self.node_id,
            "uid": self.uid,
            "callsign": self.callsign,
            "long_name": self.long_name,
            "short_name": self.short_name,
            "role": self.role,
            "team": self.team,
            "device": self.device,
            "firmware": self.firmware,
            "channel_index": self.channel_index,
            "channel_name": self.channel_name,
            "channel_hash": self.channel_hash,
            "channel_source": self.channel_source,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "altitude": self.altitude,
            "speed": self.speed,
            "course": self.course,
            "battery": self.battery,
            "voltage": self.voltage,
            "rssi": self.rssi,
            "snr": self.snr,
            "hop_count": self.hop_count,
            "position_precision": self.position_precision,
            "status": self.status(live_seconds, recent_seconds),
            "gps": self.gps_status(gps_stale_seconds),
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "last_seen_ago": round((now - self.last_seen).total_seconds(), 1),
            "last_position": self.last_position.isoformat() if self.last_position else None,
            "last_position_ago": (
                round((now - self.last_position).total_seconds(), 1) if self.last_position else None
            ),
            "rx_count": self.rx_count,
            "duplicate_count": self.duplicate_count,
            "paths": [
                {
                    "key": p["key"],
                    "transport": p["transport"],
                    "transport_label": SOURCE_LABELS.get(p["transport"], p["transport"]),
                    "count": p["count"],
                    "last_seen_ago": round((now - p["last_seen"]).total_seconds(), 1),
                }
                for p in self.paths.values()
            ],
            "routing": self.last_routing,
            "error": self.last_error,
        }


# ----------------------------------------------------------------------
# Registry: lo stato vivo, tutto in memoria
# ----------------------------------------------------------------------


class Registry:
    """Tag conosciuti + log eventi, protetti da un lock.

    Tutto in RAM: la UI non fa una query per tag a ogni refresh. Su disco
    finisce solo l'anagrafica (tabella `msh_tags`), con scrittura diluita.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.tags: OrderedDict[str, Tag] = OrderedDict()
        self.aliases: dict[str, str] = {}
        self.events = deque(maxlen=MAX_EVENTS)
        self.seq = 0
        self.rx_times = deque(maxlen=2000)
        self.unknown_channel_keys: set[str] = set()
        self.routing_errors = 0
        self.started_at = _utcnow()
        self.last_rx = None
        # Canale imparato dal feed MQTT per node id: riusato quando lo stesso
        # tag arriva via relay ATAK (dove il canale non c'è)
        self.node_channels: dict[str, dict] = {}

    # -- lookup -------------------------------------------------------

    def resolve_key(self, descriptor: dict) -> str:
        node_id = descriptor.get("node_id")
        uid = descriptor.get("uid")
        if node_id:
            # Se il tag era conosciuto solo per uid, ora lo si promuove a node id
            old = self.aliases.get(uid)
            if old and old != node_id and old in self.tags and node_id not in self.tags:
                tag = self.tags.pop(old)
                tag.key = node_id
                self.tags[node_id] = tag
            self.aliases[uid] = node_id
            return node_id
        return self.aliases.get(uid) or uid

    def get(self, key: str) -> Tag | None:
        with self._lock:
            return self.tags.get(key) or self.tags.get(self.aliases.get(key, ""))

    # -- ingest -------------------------------------------------------

    def observe(self, descriptor: dict, raw_cot: str | None = None) -> tuple[Tag, bool]:
        with self._lock:
            key = self.resolve_key(descriptor)
            tag = self.tags.get(key)
            if not tag:
                tag = Tag(key)
                self.tags[key] = tag
                while len(self.tags) > MAX_TAGS:
                    # Via il meno recente: il registry vivo non deve crescere
                    # all'infinito se qualcuno spamma uid diversi
                    dropped, _ = self.tags.popitem(last=False)
                    for uid, target in list(self.aliases.items()):
                        if target == dropped:
                            del self.aliases[uid]
            fresh = tag.apply(descriptor)
            self.rx_times.append(time.time())
            self.last_rx = _utcnow()
            return tag, fresh

    def add_packet(self, tag: Tag, packet: dict) -> None:
        with self._lock:
            tag.packets.appendleft(packet)

    def log(self, level: str, text: str, **fields) -> dict:
        with self._lock:
            self.seq += 1
            entry = {
                "seq": self.seq,
                "ts": _utcnow().isoformat(),
                "level": level,
                "text": text,
            }
            entry.update(fields)
            self.events.append(entry)
            if level == "error":
                self.routing_errors += 1
            return entry

    def events_since(self, since: int, limit: int = 300) -> list:
        with self._lock:
            return [e for e in self.events if e["seq"] > since][-limit:]

    def rx_last(self, seconds: int) -> int:
        cutoff = time.time() - seconds
        with self._lock:
            return sum(1 for t in self.rx_times if t >= cutoff)

    def snapshot(self, live_seconds: int, recent_seconds: int, gps_stale_seconds: int) -> list:
        with self._lock:
            return [
                t.serialize(live_seconds, recent_seconds, gps_stale_seconds)
                for t in self.tags.values()
            ]

    def clear_events(self) -> None:
        with self._lock:
            self.events.clear()
            self.routing_errors = 0

    def forget(self, key: str) -> None:
        with self._lock:
            key = self.aliases.get(key, key)
            self.tags.pop(key, None)
            self.unknown_channel_keys.discard(key)
            for uid, target in list(self.aliases.items()):
                if target == key:
                    del self.aliases[uid]


REGISTRY = Registry()


# ----------------------------------------------------------------------
# Canale -> gruppo
# ----------------------------------------------------------------------


def load_mappings() -> list:
    from .models import MeshChannelMap

    return db.session.query(MeshChannelMap).order_by(MeshChannelMap.id).all()


# Mappature, override e gruppi degli EUD cambiano raramente ma servono a OGNI
# pacchetto: senza cache, 100 tag che trasmettono ogni 5 s farebbero ~60
# query/s per niente. TTL corto: una modifica dalla UI si vede entro 5 s.
CONFIG_CACHE_TTL = 5.0
_cache: dict = {}


def _cached(key: str, loader):
    now = time.time()
    entry = _cache.get(key)
    if entry and now < entry[0]:
        return entry[1]
    value = loader()
    _cache[key] = (now + CONFIG_CACHE_TTL, value)
    return value


def invalidate_cache() -> None:
    _cache.clear()


def _group_name(group_id: int) -> str | None:
    """Nome corrente del gruppo: si risolve sempre dall'id, mai dalla copia."""
    from opentakserver.models.Group import Group

    group = db.session.get(Group, int(group_id))
    return group.name if group else None


def _group_id_by_name(name: str) -> int | None:
    from opentakserver.models.Group import Group

    group = db.session.query(Group).filter_by(name=name).first()
    return group.id if group else None


def eud_groups(eud_uid: str | None) -> list:
    """Gruppi OUT dell'utente che possiede quell'EUD — gli stessi che userebbe
    `route_cot()` di OTS per instradare un CoT ricevuto da lui."""
    if not eud_uid:
        return []
    from opentakserver.models.EUD import EUD
    from opentakserver.models.Group import Group
    from opentakserver.models.GroupUser import GroupUser

    eud = db.session.query(EUD).filter_by(uid=eud_uid).first()
    if not eud or not eud.user_id:
        return []
    memberships = (
        db.session.query(GroupUser)
        .filter_by(user_id=eud.user_id, direction=Group.IN, enabled=True)
        .all()
    )
    return [
        {"id": m.group_id, "name": m.group.name}
        for m in memberships
        if m.group is not None
    ]


def native_groups(descriptor: dict, source_groups: list) -> list:
    """Gruppi in cui OpenTAKServer ha GIÀ instradato questo evento da solo.

    Serve a non consegnare due volte la stessa cosa. Sono due meccanismi
    diversi a seconda della provenienza:
      - relay ATAK → `cot_parser.route_cot()` smista ai gruppi IN dell'utente
        che possiede l'EUD, oppure a `__ANON__` se non ne ha;
      - CoT generato dal meshtastic_controller di OTS → un unico gruppo fisso,
        `OTS_MESHTASTIC_GROUP` (meshtastic_controller.py:710), NON i gruppi
        dell'EUD;
      - osservazione MQTT nostra → OTS non genera nulla, nessun gruppo.
    """
    source = descriptor.get("source")
    if source == SOURCE_OTS_MESHTASTIC:
        return [_cfg("OTS_MESHTASTIC_GROUP", "Meshtastic")]
    if source == SOURCE_MQTT:
        return []
    return [g["name"] for g in source_groups] or ["__ANON__"]


def channel_for(tag: Tag, overrides: dict | None = None) -> dict:
    """Canale del tag e da dove si sa. Non inventa nulla.

    Ordine: override manuale dell'amministratore > metadato arrivato con il
    pacchetto > canale imparato via MQTT per lo stesso node id > sconosciuto.
    """
    overrides = overrides or {}
    manual = overrides.get(tag.key) or {}
    if manual.get("manual_channel_name") or manual.get("manual_channel_index") is not None:
        return {
            "index": manual.get("manual_channel_index"),
            "name": manual.get("manual_channel_name"),
            "source": "manual",
        }
    if tag.channel_name or tag.channel_index is not None:
        return {"index": tag.channel_index, "name": tag.channel_name, "source": tag.channel_source or "packet"}
    learned = REGISTRY.node_channels.get(tag.node_id or "")
    if learned:
        return {"index": learned.get("index"), "name": learned.get("name"), "source": "mqtt_correlation"}
    return {"index": None, "name": None, "source": None}


def match_mapping(channel: dict, mappings: list):
    """Prima mappatura abilitata che combacia per nome (case-insensitive) o indice."""
    name = (channel.get("name") or "").strip().lower()
    index = channel.get("index")
    for mapping in mappings:
        if not mapping.enabled:
            continue
        if name and (mapping.channel_name or "").strip().lower() == name:
            return mapping
        # Solo un indice vero (0-7) partecipa al confronto: l'hash del canale
        # non è un indice e combacerebbe per sbaglio con la mappatura di un
        # canale che non c'entra nulla.
        if is_channel_index(index) and mapping.channel_index == index:
            return mapping
    return None


def decide_route(tag: Tag, descriptor: dict, mappings: list, overrides: dict | None = None) -> dict:
    """Traccia completa della decisione di instradamento (§13 del capitolato).

    Restituisce sempre il perché, anche quando non si instrada: è la
    funzionalità che serve a diagnosticare i problemi di gruppo.
    """
    overrides = overrides or {}
    manual = overrides.get(tag.key) or {}
    channel = channel_for(tag, overrides)
    source_eud = descriptor.get("source_eud")
    source_groups = _cached(f"eud:{source_eud}", lambda: eud_groups(source_eud))

    trace = {
        "tag": tag.key,
        "callsign": tag.callsign or tag.long_name,
        "node_id": tag.node_id,
        "received_via": SOURCE_LABELS.get(descriptor.get("source"), descriptor.get("source")),
        "source_eud": descriptor.get("source_eud"),
        "source_eud_groups": [g["name"] for g in source_groups],
        # Dove OTS ha già consegnato da solo: serve a non duplicare
        "native_groups": native_groups(descriptor, source_groups),
        "channel_index": channel["index"],
        "channel_name": channel["name"],
        "channel_source": channel["source"],
        "channel_metadata_present": descriptor.get("channel_metadata_present", False),
        "mapping": None,
        "fallback": None,
        "group_id": None,
        "group_name": None,
        "result": RESULT_OBSERVED,
        "reason": "",
    }

    # Override diretto: l'amministratore ha forzato il gruppo per questo tag
    if manual.get("manual_group_id"):
        name = _group_name(manual["manual_group_id"])
        trace["group_id"] = manual["manual_group_id"]
        trace["group_name"] = name
        trace["mapping"] = "override manuale del tag"
        trace["result"] = RESULT_ROUTED if name else RESULT_ERROR
        trace["reason"] = (
            f"Gruppo forzato manualmente sul tag → {name}" if name
            else "Gruppo forzato manualmente ma non più esistente"
        )
        return trace

    mapping = match_mapping(channel, mappings) if (channel["name"] or channel["index"] is not None) else None
    if mapping:
        name = _group_name(mapping.group_id)
        trace["mapping"] = f"{mapping.channel_name or mapping.channel_index} → {name}"
        trace["group_id"] = mapping.group_id
        trace["group_name"] = name
        trace["result"] = RESULT_ROUTED if name else RESULT_ERROR
        trace["reason"] = (
            f"Canale {channel['name'] or channel['index']} mappato su {name}" if name
            else f"Mappatura verso un gruppo (id {mapping.group_id}) che non esiste più"
        )
        return trace

    # Canale sconosciuto o senza mappatura → politica di fallback
    policy = _cfg("OTS_MILSIM_MESH_FALLBACK_POLICY", "source_eud_group")
    trace["fallback"] = policy
    if channel["name"] or channel["index"] is not None:
        trace["reason"] = f"Canale {channel['name'] or channel['index']} senza mappatura → fallback {policy}"
    else:
        trace["reason"] = f"Canale non determinabile → fallback {policy}"

    if policy == "ignore":
        trace["result"] = RESULT_IGNORED
        return trace

    if policy == "source_eud_group":
        # È esattamente quello che OTS fa già da solo: non si ripubblica nulla
        trace["result"] = RESULT_NATIVE_ONLY
        trace["group_name"] = ", ".join(trace["native_groups"]) or None
        trace["reason"] += f" (già instradato da OTS a {trace['group_name']})"
        return trace

    if policy == "meshtastic_group":
        target = _cfg("OTS_MESHTASTIC_GROUP", "Meshtastic")
        group_id = _group_id_by_name(target)
        trace["group_id"] = group_id
        trace["group_name"] = target if group_id else None
        trace["result"] = RESULT_ROUTED_FALLBACK if group_id else RESULT_ERROR
        if not group_id:
            trace["reason"] += f" — gruppo «{target}» inesistente su questo server"
        return trace

    if policy == "default_group":
        group_id = _cfg("OTS_MILSIM_MESH_DEFAULT_GROUP_ID", 0)
        name = _group_name(group_id) if group_id else None
        trace["group_id"] = group_id or None
        trace["group_name"] = name
        trace["result"] = RESULT_ROUTED_FALLBACK if name else RESULT_ERROR
        if not name:
            trace["reason"] += " — nessun gruppo di default configurato"
        return trace

    trace["result"] = RESULT_ERROR
    trace["reason"] = f"Politica di fallback sconosciuta: {policy}"
    return trace


# ----------------------------------------------------------------------
# Consegna: stesso exchange e stessa routing key del cot_parser
# ----------------------------------------------------------------------


def publish_to_group(channel, group_name: str, uid: str, cot_xml: str) -> None:
    channel.basic_publish(
        exchange="groups",
        routing_key=f"{group_name}.OUT",
        body=json.dumps({"uid": uid, "cot": cot_xml}),
        properties=pika.BasicProperties(expiration=_cfg("OTS_RABBITMQ_TTL", "86400000")),
    )


def should_publish(trace: dict) -> bool:
    """Si pubblica solo se aggiunge una consegna che OTS non ha già fatto.

    OTS instrada il CoT rilanciato ai gruppi dell'EUD sorgente: se il gruppo
    mappato è già lì dentro, ripubblicare significherebbe consegnare due volte
    lo stesso evento. (Il plugin non può togliere la consegna nativa: vedi la
    limitazione nel documento di analisi.)
    """
    if trace["result"] not in (RESULT_ROUTED, RESULT_ROUTED_FALLBACK):
        return False
    if not trace.get("group_name"):
        return False
    return trace["group_name"] not in (trace.get("native_groups") or [])


# ----------------------------------------------------------------------
# Persistenza leggera dell'anagrafica
# ----------------------------------------------------------------------


PERSIST_INTERVAL = 60.0


def load_overrides() -> dict:
    """{tag_key: {manual_channel_*, manual_group_id}} dalla tabella msh_tags."""
    from .models import MeshTag

    result = {}
    for row in db.session.query(MeshTag).all():
        result[row.tag_key] = {
            "manual_channel_name": row.manual_channel_name,
            "manual_channel_index": row.manual_channel_index,
            "manual_group_id": row.manual_group_id,
            "notes": row.notes,
        }
    return result


def persist_tag(tag: Tag, force: bool = False) -> None:
    """Salva l'anagrafica del tag, al massimo una volta al minuto per tag.

    Con 100 tag che trasmettono ogni 5 s una scrittura per pacchetto sarebbe
    ~20 INSERT/s inutili: la telemetria vive in RAM, sul DB va solo l'identità.
    """
    now = time.time()
    if not force and now < tag._persist_due:
        return
    tag._persist_due = now + PERSIST_INTERVAL
    from .models import MeshTag

    try:
        row = db.session.query(MeshTag).filter_by(tag_key=tag.key).first()
        if not row:
            row = MeshTag(tag_key=tag.key, first_seen=tag.first_seen.replace(tzinfo=None))
            db.session.add(row)
        row.node_id = tag.node_id or row.node_id
        row.cot_uid = tag.uid or row.cot_uid
        row.callsign = tag.callsign or row.callsign
        row.long_name = tag.long_name or row.long_name
        row.short_name = tag.short_name or row.short_name
        row.last_seen = tag.last_seen.replace(tzinfo=None)
        db.session.commit()
    except BaseException:
        db.session.rollback()
        logger.debug(f"MilSim mesh: persistenza tag {tag.key} fallita\n{traceback.format_exc()}")


def restore_known_tags() -> int:
    """Ricarica i tag conosciuti dal DB all'avvio: la card KNOWN TAGS non
    riparte da zero a ogni restart. Restano `stale` finché non trasmettono."""
    from .models import MeshTag

    count = 0
    for row in db.session.query(MeshTag).all():
        if row.tag_key in REGISTRY.tags:
            continue
        tag = Tag(row.tag_key)
        tag.node_id = row.node_id
        tag.uid = row.cot_uid
        tag.callsign = row.callsign
        tag.long_name = row.long_name
        tag.short_name = row.short_name
        if row.first_seen:
            tag.first_seen = row.first_seen.replace(tzinfo=timezone.utc)
        if row.last_seen:
            tag.last_seen = row.last_seen.replace(tzinfo=timezone.utc)
        tag.rx_count = 0
        REGISTRY.tags[row.tag_key] = tag
        if row.cot_uid:
            REGISTRY.aliases[row.cot_uid] = row.tag_key
        count += 1
    return count


# ----------------------------------------------------------------------
# Pipeline: un CoT dal firehose -> tag -> decisione -> (eventuale) consegna
# ----------------------------------------------------------------------


def handle_cot(rabbit_channel, body: bytes) -> bool:
    """Ritorna True se il CoT era Meshtastic (e quindi è stato trattato)."""
    try:
        message = json.loads(body)
    except BaseException:
        return False
    cot_xml = message.get("cot")
    # Filtro a buon mercato prima di pagare il parse XML: il firehose porta
    # TUTTO il traffico del server, il parse di ogni evento costerebbe caro
    if not cot_xml or ("__meshtastic" not in cot_xml and "Meshtastic" not in cot_xml):
        return False

    descriptor = detect(cot_xml, message.get("uid"))
    if not descriptor:
        return False

    tag, fresh = REGISTRY.observe(descriptor, cot_xml)

    mappings = _cached("mappings", load_mappings)
    overrides = _cached("overrides", load_overrides)
    trace = decide_route(tag, descriptor, mappings, overrides)
    tag.last_routing = trace

    delivered = False
    if fresh and should_publish(trace):
        try:
            publish_to_group(rabbit_channel, trace["group_name"], descriptor["uid"], cot_xml)
            delivered = True
            tag.last_error = None
        except BaseException as e:
            trace["result"] = RESULT_ERROR
            trace["reason"] = f"Pubblicazione su {trace['group_name']}.OUT fallita: {e}"
            tag.last_error = str(e)
            logger.error(f"MilSim mesh: publish fallito per {tag.key}: {e}")

    packet = {
        "ts": _utcnow().isoformat(),
        "direction": "RX",
        "transport": descriptor["source"],
        "transport_label": SOURCE_LABELS.get(descriptor["source"], descriptor["source"]),
        "packet_type": "Position" if descriptor.get("latitude") is not None else "Status",
        "cot_type": descriptor.get("cot_type"),
        "cot_uid": descriptor.get("uid"),
        "source_eud": descriptor.get("source_eud"),
        "channel": trace["channel_name"] or (
            str(trace["channel_index"]) if trace["channel_index"] is not None else None
        ),
        "routing": trace["result"],
        "group": trace["group_name"],
        "delivered": delivered,
        "duplicate": not fresh,
        "raw_cot": sanitize_cot_xml(cot_xml),
        "latitude": descriptor.get("latitude"),
        "longitude": descriptor.get("longitude"),
    }
    REGISTRY.add_packet(tag, packet)

    label = tag.callsign or tag.long_name or tag.key
    if not fresh:
        REGISTRY.log("debug", f"duplicato ignorato per {label}", tag=tag.key,
                     channel=packet["channel"], group=trace["group_name"], source=descriptor["source"])
    else:
        channel_text = packet["channel"] or "UNKNOWN"
        REGISTRY.log(
            "error" if trace["result"] == RESULT_ERROR else "info",
            f"RX {label} · canale={channel_text} · {trace['reason']}",
            tag=tag.key, channel=channel_text, group=trace["group_name"],
            source=descriptor["source"], result=trace["result"],
        )
        if delivered:
            REGISTRY.log("info", f"CoT instradato → {trace['group_name']}.OUT", tag=tag.key,
                         channel=channel_text, group=trace["group_name"], source=descriptor["source"],
                         result=trace["result"])

    if not (trace["channel_name"] or trace["channel_index"] is not None):
        REGISTRY.unknown_channel_keys.add(tag.key)
    else:
        REGISTRY.unknown_channel_keys.discard(tag.key)

    persist_tag(tag)
    return True


# ----------------------------------------------------------------------
# Consumer firehose
# ----------------------------------------------------------------------


class _Consumer(threading.Thread):
    """Thread con connessione pika bloccante propria, riconnessione a gradini.

    Coda esclusiva e auto-delete: sparisce da sola quando il processo muore,
    non lascia code orfane che si riempiono all'infinito (problema già visto
    su questo server con le code degli EUD offline).
    """

    name_prefix = "milsim-mesh"

    def __init__(self, flask_app, name: str):
        super().__init__(name=f"{self.name_prefix}-{name}", daemon=True)
        # `name` di Thread diventa "milsim-mesh-firehose": per i log (e per il
        # grep dello script di installazione) serve l'etichetta corta
        self.label = name
        self.flask_app = flask_app
        self.stop_event = threading.Event()
        self.connected = False
        self.last_error = None
        self.messages = 0

    def connect(self):
        credentials = pika.PlainCredentials(
            self.flask_app.config.get("OTS_RABBITMQ_USERNAME"),
            self.flask_app.config.get("OTS_RABBITMQ_PASSWORD"),
        )
        connection = pika.BlockingConnection(
            pika.ConnectionParameters(
                host=self.flask_app.config.get("OTS_RABBITMQ_SERVER_ADDRESS"),
                credentials=credentials,
                heartbeat=30,
            )
        )
        return connection

    def setup(self, channel):  # pragma: no cover - sovrascritto
        raise NotImplementedError

    def on_message(self, channel, method, properties, body):  # pragma: no cover
        raise NotImplementedError

    def run(self):
        backoff = 2
        while not self.stop_event.is_set():
            connection = None
            try:
                connection = self.connect()
                channel = connection.channel()
                self.setup(channel)
                self.connected = True
                self.last_error = None
                backoff = 2
                logger.info(f"MilSim mesh: {self.label} connesso a RabbitMQ")
                while not self.stop_event.is_set():
                    connection.process_data_events(time_limit=1)
            except BaseException as e:
                self.connected = False
                self.last_error = str(e)
                logger.warning(f"MilSim mesh: {self.label} disconnesso ({e}), riprovo fra {backoff}s")
                logger.debug(traceback.format_exc())
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, 60)
            finally:
                try:
                    if connection and connection.is_open:
                        connection.close()
                except BaseException:
                    pass
        self.connected = False


class FirehoseConsumer(_Consumer):
    """Osserva ogni CoT del server e tratta quelli Meshtastic.

    `firehose` è un fanout: la coda del plugin riceve una copia propria e non
    sottrae messaggi a nessuno.
    """

    def __init__(self, flask_app):
        super().__init__(flask_app, "firehose")
        self.channel = None

    def setup(self, channel):
        self.channel = channel
        result = channel.queue_declare(queue="", exclusive=True, auto_delete=True)
        channel.queue_bind(exchange="firehose", queue=result.method.queue, routing_key="")
        channel.basic_qos(prefetch_count=50)
        channel.basic_consume(queue=result.method.queue, on_message_callback=self.on_message, auto_ack=True)

    def on_message(self, channel, method, properties, body):
        self.messages += 1
        try:
            with self.flask_app.app_context():
                handle_cot(channel, body)
        except BaseException as e:
            logger.error(f"MilSim mesh: errore nel trattare un CoT: {e}")
            logger.debug(traceback.format_exc())
            REGISTRY.log("error", f"errore nel trattare un CoT: {e}")


class MqttObserver(_Consumer):
    """Path B: legge il traffico MQTT Meshtastic grezzo, SOLO per osservare.

    Non genera CoT — quello lo fa (se abilitato) il meshtastic_controller di
    OTS. Serve a due cose che dal CoT non si ricavano: il **canale**, che sta
    nella routing key, e la telemetria radio (RSSI, SNR, hop) dal protobuf.
    Il canale imparato qui viene riusato per lo stesso node quando arriva via
    relay ATAK.
    """

    def __init__(self, flask_app):
        super().__init__(flask_app, "mqtt")
        self.decoded = 0
        self.decode_error = None

    def setup(self, channel):
        result = channel.queue_declare(queue="", exclusive=True, auto_delete=True)
        channel.queue_bind(exchange="amq.topic", queue=result.method.queue, routing_key="#")
        channel.basic_qos(prefetch_count=50)
        channel.basic_consume(queue=result.method.queue, on_message_callback=self.on_message, auto_ack=True)

    def on_message(self, channel, method, properties, body):
        self.messages += 1
        try:
            routing_key = method.routing_key or ""
            if routing_key.endswith("outgoing"):
                return
            channel_name, node_from_topic = parse_mqtt_topic(routing_key)
            info = decode_service_envelope(body)
            node_id = (info or {}).get("node_id") or node_from_topic
            if not node_id:
                return
            with self.flask_app.app_context():
                self.ingest(channel_name, node_id, info or {})
            self.decoded += 1
        except BaseException as e:
            self.decode_error = str(e)
            logger.debug(f"MilSim mesh: pacchetto MQTT non interpretabile: {e}")

    def ingest(self, channel_name: str | None, node_id: str, info: dict) -> None:
        REGISTRY.node_channels[node_id] = {
            # Dal feed MQTT il canale si conosce per NOME (sta nella routing
            # key): l'indice non arriva, quindi la correlazione va per nome.
            "name": channel_name,
            "index": info.get("channel_index"),
            "hash": info.get("channel_hash"),
            "learned_at": _utcnow().isoformat(),
        }
        descriptor = {
            "uid": node_id,
            "node_id": node_id,
            "source": SOURCE_MQTT,
            "source_eud": info.get("gateway"),
            "channel_name": channel_name,
            "channel_index": info.get("channel_index"),
            "channel_hash": info.get("channel_hash"),
            "channel_metadata_present": True,
            "cot_type": None,
            "time": info.get("packet_id"),
            "rssi": info.get("rssi"),
            "snr": info.get("snr"),
            "hop_count": info.get("hop_count"),
            "latitude": info.get("latitude"),
            "longitude": info.get("longitude"),
            "altitude": info.get("altitude"),
            "battery": info.get("battery"),
            "voltage": info.get("voltage"),
            "long_name": info.get("long_name"),
            "short_name": info.get("short_name"),
            "firmware": info.get("firmware"),
            "gps": "fix" if info.get("latitude") is not None else "none",
        }
        tag, fresh = REGISTRY.observe(descriptor)
        if not fresh:
            return
        REGISTRY.add_packet(tag, {
            "ts": _utcnow().isoformat(),
            "direction": "RX",
            "transport": SOURCE_MQTT,
            "transport_label": SOURCE_LABELS[SOURCE_MQTT],
            "packet_type": info.get("portnum") or "Mesh",
            "cot_type": None,
            "cot_uid": None,
            "source_eud": info.get("gateway"),
            "channel": channel_name,
            "routing": RESULT_OBSERVED,
            "group": None,
            "delivered": False,
            "duplicate": False,
            "raw_cot": None,
            "latitude": info.get("latitude"),
            "longitude": info.get("longitude"),
        })
        REGISTRY.log(
            "info",
            f"MQTT {node_id} · canale={channel_name or 'UNKNOWN'} · {info.get('portnum') or 'pacchetto'}",
            tag=tag.key, channel=channel_name, group=None, source=SOURCE_MQTT, result=RESULT_OBSERVED,
        )
        persist_tag(tag)


def parse_mqtt_topic(routing_key: str) -> tuple[str | None, str | None]:
    """(nome canale, node id) dalla routing key MQTT.

    Il plugin MQTT di RabbitMQ traduce `/` in `.`, quindi
    `msh/EU_868/2/e/ALPHA/!bbad0ac8` diventa `msh.EU_868.2.e.ALPHA.!bbad0ac8`.
    Si cerca il marcatore `2.e` invece di usare un indice fisso: OTS assume
    `split(".")[3]` che vale solo se OTS_MESHTASTIC_TOPIC non contiene `/`.
    """
    parts = [p for p in (routing_key or "").split(".") if p]
    for i in range(len(parts) - 2):
        if parts[i] == "2" and parts[i + 1] in ("e", "c", "json", "map"):
            channel = parts[i + 2] if len(parts) > i + 2 else None
            node = parts[i + 3] if len(parts) > i + 3 else None
            return channel, normalize_node_id(node)
    if len(parts) >= 4:
        return parts[3], normalize_node_id(parts[4]) if len(parts) > 4 else None
    return None, None


def decode_service_envelope(body: bytes) -> dict | None:
    """Telemetria radio dal protobuf Meshtastic, se le librerie ci sono.

    `meshtastic` è una dipendenza di OpenTAKServer, quindi nel venv c'è; ma il
    plugin deve restare utilizzabile anche senza (import protetto) e i pacchetti
    cifrati non si decodificano: in quel caso si tengono solo i metadati radio,
    che stanno in chiaro nell'envelope.
    """
    try:
        from meshtastic import mqtt_pb2, portnums_pb2
    except BaseException:
        return None
    try:
        envelope = mqtt_pb2.ServiceEnvelope()
        envelope.ParseFromString(body)
    except BaseException:
        return None

    packet = envelope.packet
    node_number = getattr(packet, "from")
    info = {
        "node_id": f"!{node_number:08x}" if node_number else None,
        "gateway": envelope.gateway_id or None,
        # `MeshPacket.channel` NON è l'indice del canale su questo feed: il
        # firmware ci mette l'hash. In `Router::perhapsEncode`: «Now that we
        # are encrypting the packet channel should be the hash (no longer the
        # index)», e `MQTT::onSend` pubblica proprio quel pacchetto cifrato
        # quando l'uplink è cifrato (l'impostazione di default). Anche il
        # .proto avverte che l'indice «is inherently a local concept and
        # meaningless to send between nodes»: sarebbe l'indice del gateway,
        # non del tag. Quindi l'indice qui resta ignoto — None, non 0.
        # Lo 0 del protobuf non si distingue da «campo assente» → None.
        "channel_hash": packet.channel or None,
        "channel_index": None,
        "packet_id": str(packet.id),
        "rssi": packet.rx_rssi or None,
        "snr": round(packet.rx_snr, 2) if packet.rx_snr else None,
        "hop_count": (packet.hop_start - packet.hop_limit) if packet.hop_start else None,
        "portnum": None,
    }
    try:
        if packet.HasField("decoded"):
            info["portnum"] = portnums_pb2.PortNum.Name(packet.decoded.portnum)
            _decode_payload(packet.decoded, info)
    except BaseException:
        pass
    return info


def _decode_payload(decoded, info: dict) -> None:
    """Posizione/anagrafica/telemetria dal payload in chiaro. I payload cifrati
    non arrivano qui (il campo `decoded` non c'è) e va benissimo così: il
    plugin non possiede le PSK e non deve maneggiarle."""
    from meshtastic import mesh_pb2, portnums_pb2, telemetry_pb2

    portnum = decoded.portnum
    if portnum == portnums_pb2.PortNum.POSITION_APP:
        position = mesh_pb2.Position()
        position.ParseFromString(decoded.payload)
        if position.latitude_i or position.longitude_i:
            info["latitude"] = position.latitude_i * 1e-7
            info["longitude"] = position.longitude_i * 1e-7
        if position.altitude:
            info["altitude"] = float(position.altitude)
        if position.precision_bits:
            info["position_precision"] = position.precision_bits
    elif portnum == portnums_pb2.PortNum.NODEINFO_APP:
        user = mesh_pb2.User()
        user.ParseFromString(decoded.payload)
        info["long_name"] = user.long_name or None
        info["short_name"] = user.short_name or None
        info["role"] = mesh_pb2.Config.DeviceConfig.Role.Name(user.role) if user.role else None
    elif portnum == portnums_pb2.PortNum.TELEMETRY_APP:
        telemetry = telemetry_pb2.Telemetry()
        telemetry.ParseFromString(decoded.payload)
        if telemetry.HasField("device_metrics"):
            metrics = telemetry.device_metrics
            if metrics.battery_level:
                info["battery"] = metrics.battery_level
            if metrics.voltage:
                info["voltage"] = round(metrics.voltage, 2)


# ----------------------------------------------------------------------
# Avvio / stato
# ----------------------------------------------------------------------

_consumers: dict = {}
_started = False


def start(flask_app) -> None:
    """Avviato da activate(). I plugin OTS girano solo nel processo web
    (verificato sul sorgente 1.7.13: PluginManager è istanziato solo in
    app.py), quindi basta il guard per processo: niente lease su DB."""
    global _started
    if _started:
        return
    _started = True

    if not flask_app.config.get("OTS_MILSIM_MESH_ENABLED", True):
        logger.info("MilSim mesh: disabilitato da configurazione")
        return

    try:
        with flask_app.app_context():
            restored = restore_known_tags()
        if restored:
            logger.info(f"MilSim mesh: {restored} tag conosciuti ricaricati dal DB")
    except BaseException as e:
        logger.warning(f"MilSim mesh: impossibile ricaricare i tag conosciuti: {e}")

    firehose = FirehoseConsumer(flask_app)
    firehose.start()
    _consumers["firehose"] = firehose

    if flask_app.config.get("OTS_MILSIM_MESH_MQTT_OBSERVER", False):
        mqtt = MqttObserver(flask_app)
        mqtt.start()
        _consumers["mqtt"] = mqtt
        logger.info("MilSim mesh: observer MQTT avviato")


def stop() -> None:
    """Chiamata da Plugin.stop() (anche quando il plugin viene disabilitato
    dalla web UI): ferma i consumer e riazzera il guard, cosi' un successivo
    enable_plugin() -> activate() li fa ripartire davvero."""
    global _started
    for consumer in _consumers.values():
        consumer.stop_event.set()
    _consumers.clear()
    invalidate_cache()
    _started = False


def health() -> dict:
    """Semafori per l'intestazione del monitor."""
    firehose = _consumers.get("firehose")
    mqtt = _consumers.get("mqtt")
    native_enabled = bool(_cfg("OTS_ENABLE_MESHTASTIC", False))
    return {
        "plugin": {
            "state": "ok" if firehose and firehose.connected else ("off" if not firehose else "error"),
            "detail": (firehose.last_error if firehose and not firehose.connected else "attivo")
            if firehose else "non avviato",
        },
        "rabbitmq": {
            "state": "ok" if firehose and firehose.connected else "error",
            "detail": f"{firehose.messages} messaggi osservati" if firehose else "nessun consumer",
        },
        "firehose": {
            "state": "ok" if firehose and firehose.connected else "error",
            "detail": "coda esclusiva legata a firehose" if firehose and firehose.connected else "non connesso",
        },
        "mqtt": {
            "state": "ok" if mqtt and mqtt.connected else ("off" if not mqtt else "error"),
            "detail": (
                f"{mqtt.decoded}/{mqtt.messages} pacchetti interpretati" if mqtt and mqtt.connected
                else "observer MQTT disattivato (OTS_MILSIM_MESH_MQTT_OBSERVER)" if not mqtt
                else str(mqtt.last_error)
            ),
        },
        "meshtastic_native": {
            "state": "ok" if native_enabled else "off",
            "detail": (
                f"OTS_ENABLE_MESHTASTIC attivo, gruppo unico «{_cfg('OTS_MESHTASTIC_GROUP', 'Meshtastic')}»"
                if native_enabled else "OTS_ENABLE_MESHTASTIC disattivo (normale con il relay ATAK)"
            ),
        },
    }


def thresholds() -> dict:
    return {
        "live": int(_cfg("OTS_MILSIM_MESH_LIVE_SECONDS", 60)),
        "recent": int(_cfg("OTS_MILSIM_MESH_RECENT_SECONDS", 300)),
        "gps_stale": int(_cfg("OTS_MILSIM_MESH_GPS_STALE_SECONDS", 120)),
    }
