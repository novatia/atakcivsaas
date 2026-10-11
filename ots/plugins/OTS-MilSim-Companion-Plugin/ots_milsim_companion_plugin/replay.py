# Tracce del replay (evento e partita): a chi appartiene ogni punto GPS.
#
# La tabella `points` di OTS non dice «di chi è questa posizione» in modo
# diretto: `device_uid` è chi l'ha MANDATA, `uid` è l'evento CoT. Tre casi:
#
#   PLI di un ATAK        uid == device_uid (il telefono parla di sé)
#   tag via relay ATAK    uid = tag, device_uid = il telefono che l'ha
#   (Path A)              rilanciato (cot_parser.parse_point usa il mittente)
#   tag via MQTT          device_uid = riga `euds` Meshtastic creata da OTS,
#   (Path B)              uid casuale (meshtastic_controller.position)
#
# Raggruppare per device_uid, come faceva il replay fino alla 3.27, metteva le
# posizioni dei tag rilanciati — e anche i marker piazzati dal telefono —
# dentro la traccia del telefono: il tag non compariva mai e il telefono
# «saltava». Qui la decisione è pura (niente DB) e verificata dai test.

from .mesh import normalize_node_id

TEAM_A = "team_a"
TEAM_B = "team_b"
OBSERVERS = "observers"


def user_team(memberships: list, roles: dict) -> str | None:
    """Squadra dell'utente dalle sue righe groups_users (gruppo, direzione).

    Squadra A/B: basta esserci (i giocatori hanno IN + OUT sulla propria).
    Osservatore: solo con OUT, perché IN sugli osservatori ce l'hanno anche
    i giocatori (pubblicano verso l'arbitro senza riceverlo, vedi teams.py).
    """
    groups = {group_id for group_id, _ in memberships}
    if roles.get("team_a") and roles["team_a"] in groups:
        return TEAM_A
    if roles.get("team_b") and roles["team_b"] in groups:
        return TEAM_B
    observers = set(roles.get("observers") or [])
    if any(group_id in observers and direction == "OUT" for group_id, direction in memberships):
        return OBSERVERS
    return None


class TagIndex:
    """Riconosce un tag Meshtastic da un uid qualsiasi visto in `points`.

    `tags`: righe msh_tags serializzate (tag_key, node_id, cot_uid, callsign,
    long_name, short_name, owner_user_id). `mesh_euds`: righe `euds` con
    platform Meshtastic come dict (uid, callsign, meshtastic_id, user_id).
    `relay_uids`: uid dei CoT con `<__meshtastic/>` nella finestra, per i tag
    rilanciati che il plugin non ha mai registrato.
    """

    def __init__(self, tags: list, mesh_euds: list, relay_uids=()):
        self._by_uid: dict = {}
        self._by_node: dict = {}
        self._tags: dict = {}
        for tag in tags:
            info = {
                "key": "tag:" + tag["tag_key"],
                "label": tag.get("callsign") or tag.get("long_name") or tag.get("short_name")
                or tag.get("node_id") or tag["tag_key"],
                "node_id": normalize_node_id(tag.get("node_id")) if tag.get("node_id") else None,
                "user_id": tag.get("owner_user_id"),
            }
            self._tags[info["key"]] = info
            if tag.get("cot_uid"):
                self._by_uid[tag["cot_uid"]] = info
            if info["node_id"]:
                self._by_node[info["node_id"]] = info
                self._by_uid.setdefault(info["node_id"], info)
            self._by_uid.setdefault(tag["tag_key"], info)

        for eud in mesh_euds:
            node = f"!{int(eud['meshtastic_id']) & 0xFFFFFFFF:08x}" if eud.get("meshtastic_id") is not None else None
            info = self._by_node.get(node) if node else None
            if info is None:
                info = {
                    "key": "tag:" + (node or eud["uid"]),
                    "label": eud.get("callsign") or node or eud["uid"],
                    "node_id": node,
                    "user_id": eud.get("user_id"),
                }
                self._tags[info["key"]] = info
                if node:
                    self._by_node[node] = info
            elif not info["user_id"]:
                info["user_id"] = eud.get("user_id")
            self._by_uid.setdefault(eud["uid"], info)

        for uid in relay_uids:
            if uid and uid not in self._by_uid:
                info = {"key": "tag:" + uid, "label": uid, "node_id": None, "user_id": None}
                self._tags[info["key"]] = info
                self._by_uid[uid] = info

    def lookup(self, uid: str | None) -> dict | None:
        if not uid:
            return None
        info = self._by_uid.get(uid)
        if info is None and uid.lstrip("!") and all(c in "0123456789abcdefABCDEF" for c in uid.lstrip("!")):
            # meshtastic_controller usa il node id esadecimale senza «!»
            info = self._by_node.get(normalize_node_id(uid))
        return info


def track_key(uid: str | None, device_uid: str | None, tags: TagIndex) -> tuple[str, dict | None] | None:
    """(chiave traccia, tag o None) per una riga di `points`; None = da scartare.

    Il tag ha la precedenza: un CoT relay ha uid = tag, un punto MQTT ha
    device_uid = nodo. Un punto con uid diverso dal mittente che non è un tag
    è un marker o un oggetto piazzato dall'EUD, non la sua posizione.
    """
    tag = tags.lookup(uid) or tags.lookup(device_uid)
    if tag:
        return tag["key"], tag
    if device_uid and uid == device_uid:
        return device_uid, None
    return None


def build_tracks(rows, tags: TagIndex, euds: dict, user_team_of, step: int) -> list[dict]:
    """Tracce del replay da righe (t, uid, device_uid, lat, lon, speed)
    ordinate per tempo. `euds`: uid → (callsign, user_id) degli ATAK.
    `user_team_of(user_id)` → TEAM_A/TEAM_B/OBSERVERS/None.
    Downsampling: al massimo un punto ogni `step` secondi per traccia (unisce
    anche le copie dello stesso tag ricevute da più telefoni/gateway).
    """
    tracks: dict[str, dict] = {}
    last_kept: dict[str, float] = {}
    for t, uid, device_uid, lat, lon, speed in rows:
        if lat is None or lon is None or (not lat and not lon):
            continue  # (0, 0) = nessun fix GPS
        found = track_key(uid, device_uid, tags)
        if not found:
            continue
        key, tag = found
        if key in last_kept and t - last_kept[key] < step:
            continue
        last_kept[key] = t
        track = tracks.get(key)
        if track is None:
            if tag:
                user_id = tag["user_id"]
                track = {"uid": key, "callsign": tag["label"], "kind": "tag", "node_id": tag["node_id"]}
            else:
                callsign, user_id = euds.get(key, (None, None))
                track = {"uid": key, "callsign": callsign or key, "kind": "eud"}
            track["team"] = user_team_of(user_id) if user_id else None
            track["points"] = []
            tracks[key] = track
        track["points"].append(
            [round(t, 1), round(lat, 6), round(lon, 6), round(speed, 1) if speed is not None else None]
        )

    order = {TEAM_A: 0, TEAM_B: 1, OBSERVERS: 2, None: 3}
    return sorted(
        tracks.values(),
        key=lambda tr: (order[tr["team"]], tr["kind"] != "eud", tr["callsign"].lower()),
    )
