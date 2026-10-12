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

import math

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
                "tag_key": tag["tag_key"],
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
                    "tag_key": None,
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
                info = {"key": "tag:" + uid, "tag_key": None, "label": uid, "node_id": None, "user_id": None}
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


def build_tracks(rows, tags: TagIndex, euds: dict, team_at, step: int) -> list[dict]:
    """Tracce del replay da righe (t, uid, device_uid, lat, lon, speed, course, battery)
    ordinate per tempo. Ogni punto esce come [t, lat, lon, speed, direzione, batteria]
    (direzione di marcia in gradi, vedi headings()). Ogni tag porta anche
    `relayers`: chi l'ha fatto arrivare al server, {uid telefono | "mqtt": righe}. `euds`: uid → (callsign, user_id) degli ATAK.

    `team_at(owner, t)` → (squadra o None, dallo storico?) con owner
    {"user_id": ...} per un EUD o {"tag_key": ..., "user_id": ...} per un tag
    (user_id = proprietario attuale, da usare dove lo storico non arriva).
    La squadra della traccia è quella prevalente fra i suoi punti: un cambio
    squadra a metà giocata non ricolora tutto col valore finale.

    Downsampling: al massimo un punto ogni `step` secondi per traccia (unisce
    anche le copie dello stesso tag ricevute da più telefoni/gateway).
    """
    tracks: dict[str, dict] = {}
    owners: dict[str, dict] = {}
    last_kept: dict[str, float] = {}
    courses: dict[str, list] = {}
    relayers: dict[str, dict] = {}
    for t, uid, device_uid, lat, lon, speed, course, battery in rows:
        if lat is None or lon is None or (not lat and not lon):
            continue  # (0, 0) = nessun fix GPS
        found = track_key(uid, device_uid, tags)
        if not found:
            continue
        key, tag = found
        if tag:
            # Contate tutte le righe, anche quelle che il downsampling scarta:
            # servono a sapere chi rilanciava il tag e quanto spesso
            via = "mqtt" if tags.lookup(device_uid) is tag else device_uid
            if via:
                counts = relayers.setdefault(key, {})
                counts[via] = counts.get(via, 0) + 1
        if key in last_kept and t - last_kept[key] < step:
            continue
        last_kept[key] = t
        track = tracks.get(key)
        if track is None:
            if tag:
                owners[key] = {"tag_key": tag["tag_key"], "user_id": tag["user_id"]}
                track = {"uid": key, "callsign": tag["label"], "kind": "tag", "node_id": tag["node_id"]}
            else:
                callsign, user_id = euds.get(key, (None, None))
                owners[key] = {"user_id": user_id}
                track = {"uid": key, "callsign": callsign or key, "kind": "eud"}
            track["user_id"] = owners[key]["user_id"]
            track["points"] = []
            tracks[key] = track
            courses[key] = []
        courses[key].append((course, battery))
        track["points"].append(
            [round(t, 1), round(lat, 6), round(lon, 6), round(speed, 1) if speed is not None else None]
        )

    for key, track in tracks.items():
        extra = courses[key]
        for point, heading, (_, battery) in zip(
            track["points"], headings(track["points"], [c for c, _ in extra]), extra
        ):
            # Batteria 0 = «non riportata» (OTS non distingue): meglio un buco nel grafico
            point.extend([heading, round(battery) if battery else None])
        if track["kind"] == "tag":
            track["relayers"] = relayers.get(key, {})

        votes: dict = {}
        from_current = False
        for point in track["points"]:
            team, from_history = team_at(owners[key], point[0])
            votes[team] = votes.get(team, 0) + 1
            from_current = from_current or not from_history
        # A parità vince una squadra vera su «nessuna»
        track["team"] = max(votes, key=lambda team: (votes[team], team is not None))
        track["team_from_current"] = from_current

    pair_tracks(list(tracks.values()))
    order = {TEAM_A: 0, TEAM_B: 1, OBSERVERS: 2, None: 3}
    return sorted(
        tracks.values(),
        key=lambda tr: (order[tr["team"]], tr["kind"] != "eud", tr["callsign"].lower()),
    )


def team_resolver(timeline, current_groups: dict, current_tag_owner: dict, current_roles: dict,
                  match_roles: dict | None = None):
    """team_at(owner, t) per build_tracks.

    timeline: history.Timeline. current_*: lo stato di adesso (user_id →
    [(group, dir)], tag_key → user_id, mappatura Team), usato solo prima
    dell'inizio dello storico. match_roles: le squadre salvate nella partita
    (replay partita); None = mappatura Team valida in quel momento.
    """

    def team_at(owner: dict, t: float):
        user_id = owner.get("user_id")
        tag_key = owner.get("tag_key")
        if not timeline.covers(t):
            if tag_key:
                user_id = current_tag_owner.get(tag_key)
            groups, roles = current_groups.get(user_id, []), match_roles or current_roles
        else:
            # Un tag senza anagrafica nel plugin non ha storico: vale il
            # proprietario della riga euds di OTS
            if tag_key:
                user_id = timeline.tag_owner_at(tag_key, t)
            groups, roles = timeline.groups_at(user_id, t), match_roles or timeline.roles_at(t)
        if not user_id:
            return None, timeline.covers(t)
        return user_team(groups, roles), timeline.covers(t)

    return team_at


# ----------------------------------------------------------------------
# Direzione di marcia
# ----------------------------------------------------------------------
#
# I tag non hanno bussola: si sa solo da che parte uno CAMMINA, non dove
# guarda. Due fonti, entrambe inaffidabili da fermi:
#   - `course` del GPS (Meshtastic ground_track, track/course di ATAK): buono
#     solo sopra una velocità minima, da fermo è rumore;
#   - lo spostamento fra due posizioni: buono solo quando supera l'errore del
#     GPS (qualche metro), altrimenti è il fix che balla.
# Quando nessuna delle due è affidabile si tiene l'ultima direzione buona.

MIN_SPEED = 0.7          # m/s: sotto, il course del GPS è rumore (passo lento ~1 m/s)
MIN_MOVE_M = 8.0         # m: spostamento minimo per fidarsi della direzione
MAX_GAP_S = 120.0        # oltre, la traccia è interrotta: si riparte da capo


def _distance_m(lat1, lon1, lat2, lon2) -> float:
    """Distanza approssimata (equirettangolare): esatta a sufficienza su un campo."""
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return math.hypot(x, y) * 6371000.0


def _bearing(lat1, lon1, lat2, lon2) -> float:
    """Rotta iniziale da 1 a 2, in gradi da nord in senso orario."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def headings(points: list, courses: list) -> list:
    """Direzione di marcia (gradi, intero) per ogni punto [t, lat, lon, speed, ...];
    None finché non ce n'è stata una affidabile."""
    result = []
    heading = None
    anchor = None        # (lat, lon) da cui si misura lo spostamento
    prev_t = None
    for point, course in zip(points, courses):
        t, lat, lon, speed = point[0], point[1], point[2], point[3]
        if anchor is None or t - prev_t > MAX_GAP_S:
            # Inizio o buco nella traccia: lo spostamento attraverso il buco non dice
            # da che parte camminava, si tiene la direzione e si riparte da qui
            anchor = (lat, lon)
        elif speed is not None and speed >= MIN_SPEED and course is not None and 0 < course < 360:
            # course esattamente 0 è il «non disponibile» che OTS salva al posto di 9999999
            heading = round(course) % 360
            anchor = (lat, lon)
        elif _distance_m(anchor[0], anchor[1], lat, lon) >= MIN_MOVE_M:
            heading = round(_bearing(anchor[0], anchor[1], lat, lon)) % 360
            anchor = (lat, lon)
        prev_t = t
        result.append(heading)
    return result


# ----------------------------------------------------------------------
# Radio abbinata a un telefono
# ----------------------------------------------------------------------
#
# Un telefono col plugin Meshtastic di ATAK è collegato via Bluetooth a una
# radio (spesso un tag): la radio trasmette anche la SUA posizione, che il
# telefono rilancia come un tag qualsiasi. Nei dati non c'è scritto «questa
# è la radio di quel telefono»; lo si riconosce da due fatti insieme:
#   - il tag è rilanciato (quasi solo) da quel telefono;
#   - per quasi tutta la sessione sta dove sta il telefono.
# Un tag di un altro giocatore, solo rilanciato, si muove per conto suo. Il
# caso che sfugge è un compagno che cammina accanto per tutta la giocata:
# per questo i numeri della decisione escono nella traccia e la UI li mostra.

PAIR_RADIUS_M = 30.0      # «stesso posto», con due GPS diversi
PAIR_MIN_SHARE = 0.8      # quota di campioni del tag entro il raggio
PAIR_MIN_RELAY = 0.8      # quota di righe del tag arrivate da quel telefono
PAIR_MIN_SAMPLES = 10     # sotto, troppo poco per decidere
PAIR_MAX_DT = 120.0       # posizione del telefono interpolata solo fra punti vicini nel tempo


def _position_at(points: list, t: float):
    """Posizione interpolata della traccia all'istante t, None se lì non c'è."""
    if not points or t < points[0][0] or t > points[-1][0]:
        return None
    lo, hi = 0, len(points) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if points[mid][0] <= t:
            lo = mid
        else:
            hi = mid - 1
    a = points[lo]
    if a[0] == t or lo == len(points) - 1:
        return a[1], a[2]
    b = points[lo + 1]
    if b[0] - a[0] > PAIR_MAX_DT:
        return None
    f = (t - a[0]) / (b[0] - a[0])
    return a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f


def pair_tracks(tracks: list) -> None:
    """Segna le coppie telefono ↔ radio: sul tag `paired_with` (uid del
    telefono) e `pairing` (i numeri della decisione), sul telefono
    `paired_tag`. Un telefono ha al massimo una radio: se più tag passano la
    soglia vince quello che gli sta vicino più spesso."""
    by_uid = {t["uid"]: t for t in tracks}
    candidates = []
    for tag in tracks:
        if tag["kind"] != "tag":
            continue
        relayers = tag.get("relayers") or {}
        total = sum(relayers.values())
        for phone_uid, count in relayers.items():
            phone = by_uid.get(phone_uid)
            if not phone or phone["kind"] != "eud" or count / total < PAIR_MIN_RELAY:
                continue
            distances = []
            for point in tag["points"]:
                where = _position_at(phone["points"], point[0])
                if where:
                    distances.append(_distance_m(point[1], point[2], where[0], where[1]))
            if len(distances) < PAIR_MIN_SAMPLES:
                continue
            share = sum(d <= PAIR_RADIUS_M for d in distances) / len(distances)
            if share >= PAIR_MIN_SHARE:
                distances.sort()
                candidates.append((share, tag, phone, {
                    "relay_share": round(count / total, 2),
                    "close_share": round(share, 2),
                    "median_m": round(distances[len(distances) // 2], 1),
                    "samples": len(distances),
                    "radius_m": PAIR_RADIUS_M,
                }))

    for _, tag, phone, numbers in sorted(candidates, key=lambda c: -c[0]):
        if "paired_tag" in phone or "paired_with" in tag:
            continue
        tag["paired_with"] = phone["uid"]
        tag["pairing"] = numbers
        phone["paired_tag"] = tag["uid"]
