import base64
import csv
import hashlib
import io
import json
import mimetypes
import os
import pathlib
import traceback
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import requests
import yaml
from flask import (
    Blueprint,
    Flask,
    Response,
    current_app as app,
    jsonify,
    request,
    send_from_directory,
    stream_with_context,
)
from flask_security import auth_required, current_user, roles_accepted
from sqlalchemy import insert

from opentakserver.blueprints.marti_api.data_package_marti_api import create_data_package_zip
from opentakserver.extensions import db, logger
from opentakserver.models.DataPackage import DataPackage
from opentakserver.models.Mission import Mission
from opentakserver.models.MissionChange import MissionChange
from opentakserver.models.MissionContent import MissionContent
from opentakserver.models.MissionContentMission import MissionContentMission
from opentakserver.models.user import User
from opentakserver.plugins.Plugin import Plugin

from . import cot, engine, skyfi
from .default_config import DefaultConfig
from .game_modes import GAME_MODES, MARKER_TYPES, ZONE_TYPES, serialize_registry, validate_template
from .models import (
    PLUGIN_TABLES,
    RSVP_STATUSES,
    CalendarEvent,
    EventAttendance,
    EventGuest,
    GameField,
    GameMatch,
    GameTemplate,
    Player,
    PlayerScore,
    Rank,
    SkyfiHiddenOrder,
)

import importlib.metadata

# Gradi di default (gerarchia Esercito Italiano). Modificabili dalla sezione amministrativa.
DEFAULT_RANKS = [
    ("Soldato", 0),
    ("Caporale", 30),
    ("Caporal Maggiore", 60),
    ("Sergente", 100),
    ("Sergente Maggiore", 150),
    ("Maresciallo", 210),
    ("Sottotenente", 280),
    ("Tenente", 360),
    ("Capitano", 450),
    ("Maggiore", 550),
    ("Tenente Colonnello", 660),
    ("Colonnello", 780),
    ("Generale", 910),
]

ALLOWED_BADGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}

# Margine sullo stale dei marker di partita oltre la fine, per non farli sparire
# dagli EUD mentre si sta ancora annunciando il risultato
STALE_GRACE = timedelta(minutes=2)
# Stale dei marker di una partita "pronta" (Play fatto, luce verde non ancora
# data): abbondante, all'Inizia partita vengono ripubblicati con lo stale vero
SETUP_STALE = timedelta(hours=24)


def _badges_folder() -> str:
    plugins_folder = os.path.join(app.config.get("OTS_DATA_FOLDER"), "plugins")
    folder = os.path.join(plugins_folder, "ots_milsim_companion_plugin", "badges")
    # Migrazione dal nome precedente del plugin: i badge caricati restano validi
    legacy = os.path.join(plugins_folder, "ots_eventcalendar_plugin", "badges")
    if not os.path.exists(folder) and os.path.exists(legacy):
        os.makedirs(os.path.dirname(folder), exist_ok=True)
        os.rename(legacy, folder)
    os.makedirs(folder, exist_ok=True)
    return folder


def _parse_datetime(value: str) -> datetime:
    if isinstance(value, datetime):
        return value
    value = str(value).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _get_or_create_score(player_id: int) -> PlayerScore:
    score = db.session.query(PlayerScore).filter_by(player_id=player_id).first()
    if not score:
        score = PlayerScore(player_id=player_id, score=0)
        db.session.add(score)
        db.session.flush()
    return score


def _current_player() -> Player | None:
    """Il giocatore dell'anagrafica associato all'account OTS loggato, se esiste."""
    return db.session.query(Player).filter_by(user_id=current_user.id).first()


def _set_confirmation(event_id: int, player_id: int, confirmed: bool) -> bool:
    """Conferma/revoca la presenza di un giocatore assegnando o togliendo i punti.

    Ritorna True se lo stato è cambiato. Non fa commit: lo fa il chiamante.
    """
    attendance = (
        db.session.query(EventAttendance)
        .filter_by(event_id=event_id, player_id=player_id)
        .first()
    )
    if not attendance:
        attendance = EventAttendance(event_id=event_id, player_id=player_id)
        db.session.add(attendance)
        db.session.flush()

    points = int(app.config.get("OTS_EVENTCALENDAR_POINTS_PER_PRESENCE", 10))
    score = _get_or_create_score(player_id)

    if confirmed and not attendance.confirmed:
        attendance.confirmed = True
        attendance.confirmed_by = current_user.id
        attendance.confirmed_at = datetime.utcnow()
        attendance.points_awarded = points
        score.score += points
        return True
    if not confirmed and attendance.confirmed:
        attendance.confirmed = False
        attendance.confirmed_by = None
        attendance.confirmed_at = None
        score.score = max(0, score.score - attendance.points_awarded)
        attendance.points_awarded = 0
        return True
    return False


def _rank_for_score(score_value: int, ranks: list) -> dict | None:
    best = None
    for rank in ranks:
        if rank.min_score <= score_value and (best is None or rank.min_score > best.min_score):
            best = rank
    return best.serialize() if best else None


def _resolve_rank(score_row: PlayerScore | None, ranks: list) -> dict | None:
    score_value = score_row.score if score_row else 0
    if score_row and score_row.manual_rank_id:
        manual = next((r for r in ranks if r.id == score_row.manual_rank_id), None)
        if manual:
            return manual.serialize()
    return _rank_for_score(score_value, ranks)


def _is_admin() -> bool:
    return any(role.name == "administrator" for role in current_user.roles)


def _serialize_guests(event: CalendarEvent) -> list[dict]:
    user_ids = {g.added_by for g in event.guests if g.added_by}
    usernames = {}
    if user_ids:
        for user in db.session.query(User).filter(User.id.in_(user_ids)).all():
            usernames[user.id] = user.username

    admin = _is_admin()
    guests = []
    for guest in event.guests:
        data = guest.serialize()
        data["added_by_username"] = usernames.get(guest.added_by)
        data["can_delete"] = admin or guest.added_by == current_user.id
        guests.append(data)
    return guests


def _broadcast_marker_deletions(markers) -> bool:
    """Trasmette il CoT di cancellazione (t-x-d-d) per ogni marker, come fa
    l'endpoint DELETE /api/markers di OTS, cosi' i marker spariscono anche
    dagli EUD collegati. Ritorna False se la pubblicazione su RabbitMQ fallisce."""
    try:
        import pika
        from opentakserver.functions import iso8601_string_from_datetime

        credentials = pika.PlainCredentials(
            app.config.get("OTS_RABBITMQ_USERNAME"), app.config.get("OTS_RABBITMQ_PASSWORD")
        )
        connection = pika.BlockingConnection(
            pika.ConnectionParameters(
                host=app.config.get("OTS_RABBITMQ_SERVER_ADDRESS"), credentials=credentials
            )
        )
        channel = connection.channel()

        for marker in markers:
            now = datetime.now(timezone.utc)
            event = ET.Element(
                "event",
                {
                    "how": "h-g-i-g-o",
                    "type": "t-x-d-d",
                    "version": "2.0",
                    "uid": marker.uid,
                    "start": iso8601_string_from_datetime(now),
                    "time": iso8601_string_from_datetime(now),
                    "stale": iso8601_string_from_datetime(now + timedelta(minutes=10)),
                },
            )
            ET.SubElement(
                event, "point", {"ce": "9999999", "le": "9999999", "hae": "0", "lat": "0", "lon": "0"}
            )
            detail = ET.SubElement(event, "detail")
            cot_type = marker.cot.type if marker.cot else "a-u-G"
            ET.SubElement(detail, "link", {"relation": "p-p", "uid": marker.uid, "type": cot_type})

            body = json.dumps(
                {"cot": ET.tostring(event).decode("utf-8"), "uid": app.config["OTS_NODE_ID"]}
            )
            properties = pika.BasicProperties(expiration=app.config.get("OTS_RABBITMQ_TTL"))
            channel.basic_publish(
                exchange="cot_parser", routing_key="cot_parser", body=body, properties=properties
            )
            channel.basic_publish(exchange="firehose", routing_key="", body=body, properties=properties)

        channel.close()
        connection.close()
        return True
    except BaseException as e:
        logger.error(f"MilSim maintenance: failed to broadcast marker deletions: {e}")
        logger.debug(traceback.format_exc())
        return False


def _match_field(name: str | None, default_field_id: int | None):
    """Cerca un campo da gioco per nome (case-insensitive), altrimenti usa quello di default."""
    if name:
        field = (
            db.session.query(GameField)
            .filter(db.func.lower(GameField.name) == name.strip().lower())
            .first()
        )
        if field:
            return field
    if default_field_id:
        return db.session.get(GameField, int(default_field_id))
    return None


def _import_events(rows: list[dict], source: str, default_field_id: int | None) -> dict:
    """rows: [{title, description, field_name, start, end, external_uid}]"""
    imported, skipped, errors = 0, 0, []
    for row in rows:
        try:
            uid = row.get("external_uid")
            if uid and db.session.query(CalendarEvent).filter_by(external_uid=uid).first():
                skipped += 1
                continue

            field = _match_field(row.get("field_name"), default_field_id)
            if not field:
                errors.append(f"Campo da gioco non trovato per l'evento '{row.get('title')}'")
                continue

            event = CalendarEvent(
                title=row.get("title") or "Evento senza titolo",
                description=row.get("description"),
                field_id=field.id,
                start_time=_parse_datetime(row["start"]),
                end_time=_parse_datetime(row["end"]),
                source=source,
                external_uid=uid,
            )
            db.session.add(event)
            imported += 1
        except BaseException as e:
            errors.append(f"Evento '{row.get('title')}': {e}")

    db.session.commit()
    return {"success": True, "imported": imported, "skipped": skipped, "errors": errors}


# ----------------------------------------------------------------------
# Helper modalità di gioco (template di missione e partite)
# ----------------------------------------------------------------------


def _utcnow() -> datetime:
    # Naive UTC, come i DateTime delle tabelle di OTS
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _gm_sender_uid() -> str:
    return f"GameMaster.{app.config.get('OTS_NODE_ID', 'ots')}"


def _gm_callsign() -> str:
    return app.config.get("OTS_EVENTCALENDAR_GM_CALLSIGN") or "Game Master"


def _template_payload(body: dict) -> tuple[dict | None, str | None]:
    """Valida il body JSON dell'editor e lo normalizza per il modello."""
    title = (body.get("title") or "").strip()
    if not title:
        return None, "Il titolo è obbligatorio"

    mode = body.get("mode")
    if mode not in GAME_MODES:
        return None, f"Modalità non valida: {mode}"

    try:
        duration = int(body.get("duration_minutes") or 0)
    except (TypeError, ValueError):
        return None, "Durata non valida"
    if not 1 <= duration <= 24 * 60:
        return None, "La durata deve essere tra 1 e 1440 minuti"

    markers = body.get("markers") or []
    zones = body.get("zones") or []
    errors = validate_template(mode, markers, zones, for_play=False)
    if errors:
        return None, "; ".join(errors)

    # Normalizza le coordinate nei range CoT: la mappa Leaflet dell'editor può
    # restituire longitudini "wrappate" (es. 729.88°) se trascinata su una
    # copia del mondo, e i marker risulterebbero invisibili su ATAK/OTS
    for marker in markers:
        marker["lat"], marker["lon"] = cot.wrap_coords(marker["lat"], marker["lon"])
    for zone in zones:
        zone["points"] = [list(cot.wrap_coords(p[0], p[1])) for p in zone["points"]]

    packages = [h for h in (body.get("packages") or []) if isinstance(h, str)]
    map_conf = body.get("map") or {}

    return {
        "title": title,
        "description": body.get("description"),
        "mode": mode,
        "duration_minutes": duration,
        "map_lat": map_conf.get("lat"),
        "map_lon": map_conf.get("lon"),
        "map_zoom": map_conf.get("zoom"),
        "markers_json": json.dumps(markers),
        "zones_json": json.dumps(zones),
        "packages_json": json.dumps(packages),
        "create_mission": bool(body.get("create_mission")),
    }, None


def _gps_tracks(start_utc: datetime, end_utc: datetime, step: int) -> list[dict]:
    """Tracce GPS degli EUD (tabelle points/euds di OTS) nella finestra UTC data,
    con downsampling a max un punto ogni `step` secondi per EUD. Usato dal
    replay evento (finestra dal calendario) e dal replay partita (started_at →
    ended_at tenuti dal match engine)."""
    from opentakserver.models.EUD import EUD
    from opentakserver.models.Point import Point

    callsigns = {e.uid: e.callsign for e in db.session.query(EUD).all()}

    rows = (
        db.session.query(Point)
        .filter(Point.device_uid.isnot(None))
        .filter(Point.timestamp >= start_utc.replace(tzinfo=None))
        .filter(Point.timestamp <= end_utc.replace(tzinfo=None))
        .filter(Point.latitude.isnot(None), Point.longitude.isnot(None))
        .order_by(Point.device_uid, Point.timestamp)
        .all()
    )

    tracks: dict[str, list] = {}
    last_kept: dict[str, float] = {}
    for row in rows:
        if not row.latitude and not row.longitude:
            continue  # (0, 0) = nessun fix GPS
        t = row.timestamp.replace(tzinfo=timezone.utc).timestamp()
        uid = row.device_uid
        if uid in last_kept and t - last_kept[uid] < step:
            continue
        last_kept[uid] = t
        tracks.setdefault(uid, []).append(
            [
                round(t, 1),
                round(row.latitude, 6),
                round(row.longitude, 6),
                round(row.speed, 1) if row.speed is not None else None,
            ]
        )

    return [
        {"uid": uid, "callsign": callsigns.get(uid) or uid, "points": pts}
        for uid, pts in sorted(tracks.items(), key=lambda kv: (callsigns.get(kv[0]) or kv[0]).lower())
    ]


def _match_items(match: GameMatch, uids: list[dict], targets: dict | None) -> list:
    """(CoT, destinatari) di marker e aree della partita (Play, Inizia,
    Ripubblica): ogni elemento va alla sua audience (spawn solo al proprio
    team + osservatori); targets None = broadcast a tutti."""
    snapshot = json.loads(match.snapshot_json)
    if match.ends_at:
        stale = match.ends_at.replace(tzinfo=timezone.utc) + STALE_GRACE
        when = f"fine {match.ends_at.strftime('%H:%M')} UTC"
    else:
        # Partita pronta ma non ancora iniziata: stale abbondante, verrà
        # ripubblicato con la fine vera alla luce verde
        stale = datetime.now(timezone.utc) + SETUP_STALE
        when = "in attesa della luce verde"
    remarks = f"{snapshot.get('title', match.title)} — {GAME_MODES[match.mode]['name']}, {when}"

    result = []
    items = [("marker", m) for m in snapshot.get("markers", [])] + [("zone", z) for z in snapshot.get("zones", [])]
    for entry, item in zip(uids, items):
        kind, data = item
        if kind == "marker":
            event = cot.marker_event(entry["uid"], data, stale, remarks)
        else:
            event = cot.zone_event(entry["uid"], data, stale, remarks)
        result.append((event, engine.audience_targets(targets, entry.get("audience", "all"))))
        if targets is not None:
            # Copia di persistenza: passa dal cot_parser così OTS salva il
            # marker (visibile nella web map del server). OTS la smista solo al
            # gruppo __ANON__ (EUD di utenti senza gruppi): i team, che i
            # gruppi li hanno, continuano a vedere solo la propria audience.
            result.append((event, None))
    return result


def _package_events(hashes: list[str]) -> tuple[list, list[str]]:
    """CoT b-f-t-r per i data package del template; ritorna (eventi, nomi non trovati)."""
    if not hashes:
        return [], []
    host = app.config.get("OTS_EVENTCALENDAR_GM_SERVER_ADDRESS") or request.host.split(":")[0]
    port = app.config.get("OTS_MARTI_HTTPS_PORT") or 8443
    events, missing = [], []
    for file_hash in hashes:
        package = db.session.execute(db.session.query(DataPackage).filter_by(hash=file_hash)).scalar()
        if not package:
            missing.append(file_hash)
            continue
        sender_url = f"https://{host}:{port}/Marti/api/sync/metadata/{file_hash}/tool"
        events.append(
            cot.fileshare_event(
                {"filename": package.filename, "hash": package.hash, "size": package.size},
                sender_url,
                _gm_sender_uid(),
                _gm_callsign(),
            )
        )
    return events, missing


class MilSimCompanionPlugin(Plugin):
    metadata = pathlib.Path(__file__).resolve().parent.name
    url_prefix = f"/api/plugins/{metadata.lower()}"
    blueprint = Blueprint("MilSimCompanionPlugin", __name__, url_prefix=url_prefix)

    def activate(self, app: Flask, enabled: bool = True):
        self._app = app
        self._load_config()
        self.load_metadata()

        try:
            with app.app_context():
                # Migrazione v1 -> v2: presenze/punteggi passano da user_id a player_id
                # (anagrafica giocatori). Salva i vecchi dati, ricrea le tabelle e
                # reimporta mappando ogni utente su un giocatore creato automaticamente.
                from sqlalchemy import inspect as sqla_inspect, text

                inspector = sqla_inspect(db.engine)
                legacy_attendance, legacy_scores = [], []
                legacy = inspector.has_table("ec_attendances") and "user_id" in [
                    c["name"] for c in inspector.get_columns("ec_attendances")
                ]
                if legacy:
                    logger.info("MilSim: migrating attendance/scores from users to players")
                    legacy_attendance = db.session.execute(
                        text(
                            "SELECT event_id, user_id, rsvp_status, confirmed, confirmed_by,"
                            " confirmed_at, points_awarded FROM ec_attendances"
                        )
                    ).fetchall()
                    if inspector.has_table("ec_user_scores"):
                        legacy_scores = db.session.execute(
                            text("SELECT user_id, score, manual_rank_id FROM ec_user_scores")
                        ).fetchall()
                    db.session.execute(text("DROP TABLE ec_attendances"))
                    db.session.execute(text("DROP TABLE IF EXISTS ec_user_scores"))
                    db.session.commit()

                # Crea le tabelle del plugin se non esistono (non tocca le tabelle di OTS)
                db.metadata.create_all(bind=db.engine, tables=PLUGIN_TABLES, checkfirst=True)

                # Migrazione 3.1 -> 3.2: gm_matches acquisisce il ciclo di vita
                # pronta/in corso/terminata (created_at, end_reason, winner;
                # started_at/ends_at diventano null finché non si dà il via)
                if inspector.has_table("gm_matches"):
                    match_columns = {c["name"] for c in inspector.get_columns("gm_matches")}
                    added = []
                    for name, ddl in (
                        ("created_at", "ALTER TABLE gm_matches ADD COLUMN created_at TIMESTAMP"),
                        ("end_reason", "ALTER TABLE gm_matches ADD COLUMN end_reason VARCHAR(32)"),
                        ("winner", "ALTER TABLE gm_matches ADD COLUMN winner VARCHAR(255)"),
                        # 3.2 -> 3.6: destinatari = gruppi ATAK (groups.id di OTS)
                        ("team_a_id", "ALTER TABLE gm_matches ADD COLUMN team_a_id INTEGER"),
                        ("team_b_id", "ALTER TABLE gm_matches ADD COLUMN team_b_id INTEGER"),
                        ("observers_json", "ALTER TABLE gm_matches ADD COLUMN observers_json TEXT"),
                        # 3.7: missione Data Sync collegata alla partita
                        ("mission_name", "ALTER TABLE gm_matches ADD COLUMN mission_name VARCHAR(255)"),
                    ):
                        if name not in match_columns:
                            db.session.execute(text(ddl))
                            added.append(name)
                    if added:
                        db.session.execute(
                            text("UPDATE gm_matches SET created_at = started_at WHERE created_at IS NULL")
                        )
                        db.session.commit()
                        logger.info(f"MilSim: gm_matches migrata (aggiunte colonne {', '.join(added)})")
                    # Postgres: i vecchi NOT NULL su started_at/ends_at vanno tolti
                    # (su SQLite l'ALTER non esiste: il vincolo resta solo formale)
                    for ddl in (
                        "ALTER TABLE gm_matches ALTER COLUMN started_at DROP NOT NULL",
                        "ALTER TABLE gm_matches ALTER COLUMN ends_at DROP NOT NULL",
                        # 3.3 -> 3.4: l'anagrafica gruppi custom è stata ritirata
                        # (si usano i team nativi di ATAK): via tabelle e colonne
                        "DROP TABLE IF EXISTS gm_group_euds",
                        "DROP TABLE IF EXISTS gm_groups",
                        "ALTER TABLE gm_matches DROP COLUMN IF EXISTS team_a_group_id",
                        "ALTER TABLE gm_matches DROP COLUMN IF EXISTS team_b_group_id",
                    ):
                        try:
                            db.session.execute(text(ddl))
                            db.session.commit()
                        except BaseException:
                            db.session.rollback()

                # 3.6 -> 3.7: flag "crea missione" sui template
                if inspector.has_table("gm_templates"):
                    template_columns = {c["name"] for c in inspector.get_columns("gm_templates")}
                    if "create_mission" not in template_columns:
                        db.session.execute(text("ALTER TABLE gm_templates ADD COLUMN create_mission BOOLEAN"))
                        db.session.execute(text("UPDATE gm_templates SET create_mission = FALSE WHERE create_mission IS NULL"))
                        db.session.commit()
                        logger.info("MilSim: gm_templates migrata (aggiunta colonna create_mission)")

                if legacy:
                    # Un giocatore per ogni account OTS esistente, già associato
                    linked = {p.user_id for p in db.session.query(Player).all() if p.user_id}
                    for user in db.session.query(User).all():
                        if user.id not in linked:
                            db.session.add(
                                Player(callsign=user.username, user_id=user.id, active=user.active)
                            )
                    db.session.commit()

                    players_by_user = {
                        p.user_id: p.id for p in db.session.query(Player).all() if p.user_id
                    }
                    for row in legacy_attendance:
                        player_id = players_by_user.get(row.user_id)
                        if player_id:
                            db.session.add(
                                EventAttendance(
                                    event_id=row.event_id,
                                    player_id=player_id,
                                    rsvp_status=row.rsvp_status,
                                    confirmed=row.confirmed,
                                    confirmed_by=row.confirmed_by,
                                    confirmed_at=row.confirmed_at,
                                    points_awarded=row.points_awarded,
                                )
                            )
                    for row in legacy_scores:
                        player_id = players_by_user.get(row.user_id)
                        if player_id:
                            db.session.add(
                                PlayerScore(
                                    player_id=player_id,
                                    score=row.score,
                                    manual_rank_id=row.manual_rank_id,
                                )
                            )
                    db.session.commit()
                    logger.info(
                        f"MilSim: migrated {len(legacy_attendance)} attendance rows and "
                        f"{len(legacy_scores)} score rows to players"
                    )

                # Seed dei gradi di default alla prima attivazione
                if not db.session.query(Rank).first():
                    for name, min_score in DEFAULT_RANKS:
                        db.session.add(Rank(name=name, min_score=min_score))
                    db.session.commit()
                    logger.info("MilSim: seeded default ranks")

            if not app.config.get("OTS_SKYFI_PLUGIN_API_KEY"):
                logger.warning(f"{self.name}: API key SkyFi non configurata (OTS_SKYFI_PLUGIN_API_KEY): il tab SkyFi non funzionerà")

            # Match engine: tiene il tempo delle partite (tick 1 s) e le chiude
            # allo scadere; il lease su DB garantisce una sola istanza attiva
            engine.start_engine(app)

            logger.info(f"Successfully Loaded {self.name}")
        except BaseException as e:
            logger.error(f"Failed to load {self.name}: {e}")
            logger.error(traceback.format_exc())

    # Do not change this
    def load_metadata(self):
        try:
            self.distro = pathlib.Path(__file__).resolve().parent.name
            self.metadata = importlib.metadata.metadata(self.distro).json
            self.name = self.metadata["name"]
            self.metadata["distro"] = self.distro
            return self.metadata
        except BaseException as e:
            logger.error(e)
            logger.debug(traceback.format_exc())
            return None

    # Loads default config and user config from ~/ots/config.yml
    def _load_config(self):
        for key in dir(DefaultConfig):
            if key.isupper():
                self._config[key] = getattr(DefaultConfig, key)
                self._app.config.update({key: getattr(DefaultConfig, key)})

        with open(os.path.join(self._app.config.get("OTS_DATA_FOLDER"), "config.yml")) as yaml_file:
            yaml_config = yaml.safe_load(yaml_file)
            for key in self._config.keys():
                value = yaml_config.get(key)
                if value is not None:
                    self._config[key] = value
                    self._app.config.update({key: value})

    def get_info(self):
        self.load_metadata()
        self.get_plugin_routes(self.url_prefix)
        return {"name": self.name, "distro": self.distro, "routes": self.routes}

    def stop(self):
        pass

    # ------------------------------------------------------------------
    # Rotte standard del template (info, UI, config)
    # ------------------------------------------------------------------

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/")
    def plugin_info():
        try:
            distribution = None
            distributions = importlib.metadata.packages_distributions()
            for distro in distributions:
                if str(__name__).startswith(distro):
                    distribution = distributions[distro][0]
                    break

            if distribution:
                info = importlib.metadata.metadata(distribution)
                return jsonify(info.json)
            else:
                return jsonify({"success": False, "error": "Plugin not found"}), 404
        except BaseException as e:
            logger.error(e)
            return jsonify({"success": False, "error": str(e)}), 500

    # La shell HTML della UI è pubblica così i link condivisi (es. WhatsApp) si aprono
    # sempre; i dati restano protetti dalle API (auth_required/roles_accepted) e la
    # pagina mostra l'invito al login se l'utente non è autenticato.
    @staticmethod
    @blueprint.route("/ui")
    def ui():
        return send_from_directory(
            f"../{pathlib.Path(__file__).parent.resolve().name}/ui", "index.html", as_attachment=False
        )

    @staticmethod
    @blueprint.route("/assets/<file_name>")
    @blueprint.route("/ui/<file_name>")
    def serve(file_name):
        if file_name and os.path.exists(
            os.path.join(pathlib.Path(__file__).parent.resolve(), "ui", "assets", file_name)
        ):
            return send_from_directory(
                f"../{pathlib.Path(__file__).parent.resolve().name}/ui/assets", file_name
            )
        elif file_name and os.path.exists(
            os.path.join(pathlib.Path(__file__).parent.resolve(), "ui", file_name)
        ):
            return send_from_directory(f"../{pathlib.Path(__file__).parent.resolve().name}/ui", file_name)
        else:
            return "", 404

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/config")
    def config():
        config = {}
        for key in dir(DefaultConfig):
            if key.isupper():
                config[key] = app.config.get(key)
        return jsonify(config)

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/config", methods=["POST"])
    def update_config():
        try:
            result = DefaultConfig.update_config(request.json)
            if result["success"]:
                return jsonify(result)
            else:
                return jsonify(result), 400
        except BaseException as e:
            logger.error("Failed to update config:" + str(e))
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 400

    # ------------------------------------------------------------------
    # Profilo corrente
    # ------------------------------------------------------------------

    @staticmethod
    @auth_required()
    @blueprint.route("/me")
    def me():
        try:
            ranks = db.session.query(Rank).order_by(Rank.min_score).all()
            player = _current_player()
            score_row = player.score_row if player else None
            return jsonify(
                {
                    "user_id": current_user.id,
                    "username": current_user.username,
                    "roles": [role.name for role in current_user.roles],
                    "player": player.serialize() if player else None,
                    "score": score_row.score if score_row else 0,
                    "rank": _resolve_rank(score_row, ranks),
                }
            )
        except BaseException as e:
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    # ------------------------------------------------------------------
    # Anagrafica campi da gioco
    # ------------------------------------------------------------------

    @staticmethod
    @auth_required()
    @blueprint.route("/fields")
    def get_fields():
        try:
            fields = db.session.query(GameField).order_by(GameField.name).all()
            return jsonify([f.serialize() for f in fields])
        except BaseException as e:
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/fields", methods=["POST"])
    def create_field():
        try:
            data = request.json
            if not data.get("name"):
                return jsonify({"success": False, "error": "Il nome del campo è obbligatorio"}), 400

            field = GameField(
                name=data["name"],
                address=data.get("address"),
                latitude=data.get("latitude"),
                longitude=data.get("longitude"),
                description=data.get("description"),
                active=data.get("active", True),
            )
            db.session.add(field)
            db.session.commit()
            return jsonify({"success": True, "field": field.serialize()})
        except BaseException as e:
            db.session.rollback()
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 400

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/fields/<int:field_id>", methods=["PUT"])
    def update_field(field_id):
        try:
            field = db.session.get(GameField, field_id)
            if not field:
                return jsonify({"success": False, "error": "Campo non trovato"}), 404

            data = request.json
            for attr in ("name", "address", "latitude", "longitude", "description", "active"):
                if attr in data:
                    setattr(field, attr, data[attr])
            db.session.commit()
            return jsonify({"success": True, "field": field.serialize()})
        except BaseException as e:
            db.session.rollback()
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 400

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/fields/<int:field_id>", methods=["DELETE"])
    def delete_field(field_id):
        try:
            field = db.session.get(GameField, field_id)
            if not field:
                return jsonify({"success": False, "error": "Campo non trovato"}), 404
            if field.events:
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": "Il campo ha eventi associati: disattivalo invece di eliminarlo",
                        }
                    ),
                    400,
                )
            db.session.delete(field)
            db.session.commit()
            return jsonify({"success": True})
        except BaseException as e:
            db.session.rollback()
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 400

    # ------------------------------------------------------------------
    # Eventi
    # ------------------------------------------------------------------

    # L'elenco completo del calendario è riservato agli admin: gli operatori
    # accedono al singolo evento tramite il link condiviso (GET /events/<id>)
    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/events")
    def get_events():
        try:
            query = db.session.query(CalendarEvent)
            if request.args.get("from"):
                query = query.filter(CalendarEvent.end_time >= _parse_datetime(request.args["from"]))
            if request.args.get("to"):
                query = query.filter(CalendarEvent.start_time <= _parse_datetime(request.args["to"]))
            events = query.order_by(CalendarEvent.start_time).all()

            player = _current_player()
            results = []
            for event in events:
                data = event.serialize()
                counts = {"present": 0, "absent": 0, "maybe": 0, "confirmed": 0}
                my_rsvp = "not_configured"
                for attendance in event.attendances:
                    if attendance.rsvp_status in counts:
                        counts[attendance.rsvp_status] += 1
                    if attendance.confirmed:
                        counts["confirmed"] += 1
                    if player and attendance.player_id == player.id:
                        my_rsvp = attendance.rsvp_status
                data["counts"] = counts
                data["my_rsvp"] = my_rsvp
                results.append(data)
            return jsonify(results)
        except BaseException as e:
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    # Dettaglio di un singolo evento (per la vista dedicata del link condiviso)
    @staticmethod
    @auth_required()
    @blueprint.route("/events/<int:event_id>")
    def get_event(event_id):
        try:
            event = db.session.get(CalendarEvent, event_id)
            if not event:
                return jsonify({"success": False, "error": "Evento non trovato"}), 404

            player = _current_player()
            data = event.serialize()
            counts = {"present": 0, "absent": 0, "maybe": 0, "confirmed": 0}
            my_rsvp = "not_configured"
            for attendance in event.attendances:
                if attendance.rsvp_status in counts:
                    counts[attendance.rsvp_status] += 1
                if attendance.confirmed:
                    counts["confirmed"] += 1
                if player and attendance.player_id == player.id:
                    my_rsvp = attendance.rsvp_status
            data["counts"] = counts
            data["my_rsvp"] = my_rsvp
            data["has_player"] = player is not None
            data["guests"] = _serialize_guests(event)
            return jsonify(data)
        except BaseException as e:
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/events", methods=["POST"])
    def create_event():
        try:
            data = request.json
            for required in ("title", "field_id", "start_time", "end_time"):
                if not data.get(required):
                    return jsonify({"success": False, "error": f"{required} è obbligatorio"}), 400

            start = _parse_datetime(data["start_time"])
            end = _parse_datetime(data["end_time"])
            if end <= start:
                return jsonify({"success": False, "error": "La fine deve essere dopo l'inizio"}), 400

            if not db.session.get(GameField, int(data["field_id"])):
                return jsonify({"success": False, "error": "Campo da gioco non trovato"}), 400

            event = CalendarEvent(
                title=data["title"],
                description=data.get("description"),
                field_id=int(data["field_id"]),
                start_time=start,
                end_time=end,
                source="manual",
            )
            db.session.add(event)
            db.session.commit()
            return jsonify({"success": True, "event": event.serialize()})
        except BaseException as e:
            db.session.rollback()
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 400

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/events/<int:event_id>", methods=["PUT"])
    def update_event(event_id):
        try:
            event = db.session.get(CalendarEvent, event_id)
            if not event:
                return jsonify({"success": False, "error": "Evento non trovato"}), 404

            data = request.json
            if "title" in data:
                event.title = data["title"]
            if "description" in data:
                event.description = data["description"]
            if "field_id" in data:
                if not db.session.get(GameField, int(data["field_id"])):
                    return jsonify({"success": False, "error": "Campo da gioco non trovato"}), 400
                event.field_id = int(data["field_id"])
            if "start_time" in data:
                event.start_time = _parse_datetime(data["start_time"])
            if "end_time" in data:
                event.end_time = _parse_datetime(data["end_time"])
            if event.end_time <= event.start_time:
                db.session.rollback()
                return jsonify({"success": False, "error": "La fine deve essere dopo l'inizio"}), 400

            db.session.commit()
            return jsonify({"success": True, "event": event.serialize()})
        except BaseException as e:
            db.session.rollback()
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 400

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/events/<int:event_id>", methods=["DELETE"])
    def delete_event(event_id):
        try:
            event = db.session.get(CalendarEvent, event_id)
            if not event:
                return jsonify({"success": False, "error": "Evento non trovato"}), 404

            # Riallinea i punteggi delle presenze già confermate
            for attendance in event.attendances:
                if attendance.confirmed and attendance.points_awarded:
                    score = _get_or_create_score(attendance.player_id)
                    score.score = max(0, score.score - attendance.points_awarded)

            db.session.delete(event)
            db.session.commit()
            return jsonify({"success": True})
        except BaseException as e:
            db.session.rollback()
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 400

    # ------------------------------------------------------------------
    # RSVP utente: presente / non presente / in dubbio / non configurato
    # ------------------------------------------------------------------

    @staticmethod
    @auth_required()
    @blueprint.route("/events/<int:event_id>/rsvp", methods=["POST"])
    def rsvp(event_id):
        try:
            status = (request.json or {}).get("status")
            if status not in RSVP_STATUSES:
                return (
                    jsonify({"success": False, "error": f"status deve essere uno di {RSVP_STATUSES}"}),
                    400,
                )

            event = db.session.get(CalendarEvent, event_id)
            if not event:
                return jsonify({"success": False, "error": "Evento non trovato"}), 404

            player = _current_player()
            if not player:
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": "Il tuo account non è associato a nessun giocatore: "
                            "chiedi a un amministratore di associarti nell'anagrafica Giocatori.",
                        }
                    ),
                    400,
                )

            attendance = (
                db.session.query(EventAttendance)
                .filter_by(event_id=event_id, player_id=player.id)
                .first()
            )
            if not attendance:
                attendance = EventAttendance(event_id=event_id, player_id=player.id)
                db.session.add(attendance)
            attendance.rsvp_status = status
            db.session.commit()
            return jsonify({"success": True, "attendance": attendance.serialize()})
        except BaseException as e:
            db.session.rollback()
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 400

    # ------------------------------------------------------------------
    # Ospiti "in prova" (nome e cognome, senza account)
    # ------------------------------------------------------------------

    @staticmethod
    @auth_required()
    @blueprint.route("/events/<int:event_id>/guests", methods=["POST"])
    def add_guest(event_id):
        try:
            event = db.session.get(CalendarEvent, event_id)
            if not event:
                return jsonify({"success": False, "error": "Evento non trovato"}), 404

            data = request.json or {}
            first_name = (data.get("first_name") or "").strip()
            last_name = (data.get("last_name") or "").strip()
            if not first_name or not last_name:
                return jsonify({"success": False, "error": "Nome e cognome sono obbligatori"}), 400

            duplicate = (
                db.session.query(EventGuest)
                .filter(
                    EventGuest.event_id == event_id,
                    db.func.lower(EventGuest.first_name) == first_name.lower(),
                    db.func.lower(EventGuest.last_name) == last_name.lower(),
                )
                .first()
            )
            if duplicate:
                return jsonify({"success": False, "error": "Ospite già registrato per questo evento"}), 400

            guest = EventGuest(
                event_id=event_id,
                first_name=first_name,
                last_name=last_name,
                added_by=current_user.id,
            )
            db.session.add(guest)
            db.session.commit()
            return jsonify({"success": True, "guest": guest.serialize()})
        except BaseException as e:
            db.session.rollback()
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 400

    @staticmethod
    @auth_required()
    @blueprint.route("/guests/<int:guest_id>", methods=["DELETE"])
    def delete_guest(guest_id):
        try:
            guest = db.session.get(EventGuest, guest_id)
            if not guest:
                return jsonify({"success": False, "error": "Ospite non trovato"}), 404
            # Può eliminare solo chi l'ha registrato, oppure un admin
            if guest.added_by != current_user.id and not _is_admin():
                return jsonify({"success": False, "error": "Non autorizzato"}), 403

            db.session.delete(guest)
            db.session.commit()
            return jsonify({"success": True})
        except BaseException as e:
            db.session.rollback()
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 400

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/guests/<int:guest_id>/confirm", methods=["POST"])
    def confirm_guest(guest_id):
        try:
            guest = db.session.get(EventGuest, guest_id)
            if not guest:
                return jsonify({"success": False, "error": "Ospite non trovato"}), 404

            confirmed = bool((request.json or {}).get("confirmed"))
            guest.confirmed = confirmed
            guest.confirmed_by = current_user.id if confirmed else None
            guest.confirmed_at = datetime.utcnow() if confirmed else None
            db.session.commit()
            return jsonify({"success": True, "guest": guest.serialize()})
        except BaseException as e:
            db.session.rollback()
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 400

    # ------------------------------------------------------------------
    # Conferma presenze (admin sul campo)
    # ------------------------------------------------------------------

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/events/<int:event_id>/attendance")
    def event_attendance(event_id):
        try:
            event = db.session.get(CalendarEvent, event_id)
            if not event:
                return jsonify({"success": False, "error": "Evento non trovato"}), 404

            attendance_by_player = {a.player_id: a for a in event.attendances}
            players = (
                db.session.query(Player)
                .filter_by(active=True)
                .order_by(Player.last_name, Player.first_name, Player.callsign)
                .all()
            )

            results = []
            for player in players:
                attendance = attendance_by_player.get(player.id)
                results.append(
                    {
                        "player_id": player.id,
                        "display_name": player.formal_name(),
                        "rsvp_status": attendance.rsvp_status if attendance else "not_configured",
                        "confirmed": attendance.confirmed if attendance else False,
                    }
                )
            return jsonify(
                {
                    "event": event.serialize(),
                    "attendance": results,
                    "guests": _serialize_guests(event),
                }
            )
        except BaseException as e:
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/events/<int:event_id>/attendance", methods=["POST"])
    def confirm_attendance(event_id):
        try:
            data = request.json or {}
            player_id = data.get("player_id")
            confirmed = bool(data.get("confirmed"))
            if not player_id:
                return jsonify({"success": False, "error": "player_id è obbligatorio"}), 400

            event = db.session.get(CalendarEvent, event_id)
            if not event:
                return jsonify({"success": False, "error": "Evento non trovato"}), 404
            if not db.session.get(Player, int(player_id)):
                return jsonify({"success": False, "error": "Giocatore non trovato"}), 404

            _set_confirmation(event_id, int(player_id), confirmed)
            db.session.commit()
            score = db.session.query(PlayerScore).filter_by(player_id=int(player_id)).first()
            return jsonify({"success": True, "score": score.score if score else 0})
        except BaseException as e:
            db.session.rollback()
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 400

    # Segna tutta la squadra (giocatori attivi) come presente sull'evento
    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/events/<int:event_id>/attendance/all", methods=["POST"])
    def confirm_all_attendance(event_id):
        try:
            confirmed = bool((request.json or {}).get("confirmed", True))
            event = db.session.get(CalendarEvent, event_id)
            if not event:
                return jsonify({"success": False, "error": "Evento non trovato"}), 404

            players = db.session.query(Player).filter_by(active=True).all()
            changed = 0
            for player in players:
                if _set_confirmation(event_id, player.id, confirmed):
                    changed += 1
            db.session.commit()
            logger.info(
                f"MilSim: {current_user.username} set confirmed={confirmed} "
                f"for {changed} players on event {event_id}"
            )
            return jsonify({"success": True, "changed": changed})
        except BaseException as e:
            db.session.rollback()
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 400

    # ------------------------------------------------------------------
    # Replay giocata: tracce GPS degli EUD durante l'evento
    # ------------------------------------------------------------------

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/events/<int:event_id>/replay")
    def event_replay(event_id):
        """Tracce GPS registrate nelle tabelle points/euds di OTS nella finestra dell'evento.

        Gli orari degli eventi sono in ora locale (OTS_EVENTCALENDAR_TIMEZONE, il fuso
        del sistema puo' essere UTC), i punti CoT sono salvati in UTC: la conversione
        avviene qui. ?step=N tiene al massimo un punto ogni N secondi per EUD (default 5).
        """
        try:
            from zoneinfo import ZoneInfo

            event = db.session.get(CalendarEvent, event_id)
            if not event:
                return jsonify({"success": False, "error": "Evento non trovato"}), 404

            try:
                step = max(0, int(request.args.get("step", 5)))
            except ValueError:
                step = 5

            tz = ZoneInfo(app.config.get("OTS_EVENTCALENDAR_TIMEZONE", "Europe/Rome"))
            start_utc = event.start_time.replace(tzinfo=tz).astimezone(timezone.utc)
            end_utc = event.end_time.replace(tzinfo=tz).astimezone(timezone.utc)

            return jsonify(
                {
                    "event": event.serialize(),
                    "start": start_utc.timestamp(),
                    "end": end_utc.timestamp(),
                    "step": step,
                    "tracks": _gps_tracks(start_utc, end_utc, step),
                }
            )
        except BaseException as e:
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    # ------------------------------------------------------------------
    # Import: CSV e Google Calendar (iCal/ICS)
    # ------------------------------------------------------------------

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/import/csv", methods=["POST"])
    def import_csv():
        """CSV con intestazione: title,description,field,start,end (separatore , o ;).

        Le date sono in formato ISO (es. 2026-10-04T09:00). La colonna field è il nome
        del campo da gioco; se non corrisponde si usa default_field_id (parametro form).
        """
        try:
            if "file" not in request.files:
                return jsonify({"success": False, "error": "Nessun file caricato"}), 400

            content = request.files["file"].read().decode("utf-8-sig")
            try:
                dialect = csv.Sniffer().sniff(content.splitlines()[0], delimiters=",;")
            except BaseException:
                dialect = csv.excel

            reader = csv.DictReader(io.StringIO(content), dialect=dialect)
            rows = []
            for line in reader:
                line = {(k or "").strip().lower(): (v or "").strip() for k, v in line.items()}
                if not line.get("title") and not line.get("start"):
                    continue
                rows.append(
                    {
                        "title": line.get("title"),
                        "description": line.get("description"),
                        "field_name": line.get("field"),
                        "start": line.get("start"),
                        "end": line.get("end"),
                        "external_uid": None,
                    }
                )

            default_field_id = request.form.get("default_field_id")
            result = _import_events(rows, "csv", default_field_id)
            return jsonify(result)
        except BaseException as e:
            db.session.rollback()
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 400

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/import/ics", methods=["POST"])
    def import_ics():
        """Importa da un URL iCal (es. l'indirizzo ICS di un Google Calendar) o da un file .ics.

        Gli eventi sono deduplicati tramite lo UID iCal: rilanciare l'import aggiunge
        solo gli eventi nuovi. LOCATION viene confrontata con i nomi dei campi da gioco,
        altrimenti si usa default_field_id.
        """
        try:
            import icalendar

            ics_data = None
            default_field_id = None

            if request.files and "file" in request.files:
                ics_data = request.files["file"].read()
                default_field_id = request.form.get("default_field_id")
            else:
                payload = request.json or {}
                default_field_id = payload.get("default_field_id")
                url = payload.get("url")
                if url:
                    import httpx

                    response = httpx.get(url, follow_redirects=True, timeout=30)
                    response.raise_for_status()
                    ics_data = response.content

            if not ics_data:
                return jsonify({"success": False, "error": "Fornisci un URL ICS o un file .ics"}), 400

            calendar = icalendar.Calendar.from_ical(ics_data)
            rows = []
            for component in calendar.walk("VEVENT"):
                start = component.get("DTSTART")
                end = component.get("DTEND") or start
                if not start:
                    continue

                start_dt = start.dt
                end_dt = end.dt
                # Eventi di tutto il giorno: icalendar restituisce date, non datetime
                if not isinstance(start_dt, datetime):
                    start_dt = datetime(start_dt.year, start_dt.month, start_dt.day, 0, 0)
                if not isinstance(end_dt, datetime):
                    end_dt = datetime(end_dt.year, end_dt.month, end_dt.day, 23, 59)
                if start_dt.tzinfo is not None:
                    start_dt = start_dt.astimezone().replace(tzinfo=None)
                if end_dt.tzinfo is not None:
                    end_dt = end_dt.astimezone().replace(tzinfo=None)
                if end_dt <= start_dt:
                    end_dt = start_dt.replace(hour=23, minute=59)

                rows.append(
                    {
                        "title": str(component.get("SUMMARY", "Evento senza titolo")),
                        "description": str(component.get("DESCRIPTION", "")) or None,
                        "field_name": str(component.get("LOCATION", "")) or None,
                        "start": start_dt,
                        "end": end_dt,
                        "external_uid": str(component.get("UID")) if component.get("UID") else None,
                    }
                )

            result = _import_events(rows, "ics", default_field_id)
            return jsonify(result)
        except BaseException as e:
            db.session.rollback()
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 400

    # ------------------------------------------------------------------
    # Gradi (ranks) e badge
    # ------------------------------------------------------------------

    @staticmethod
    @auth_required()
    @blueprint.route("/ranks")
    def get_ranks():
        try:
            ranks = db.session.query(Rank).order_by(Rank.min_score).all()
            return jsonify([r.serialize() for r in ranks])
        except BaseException as e:
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/ranks", methods=["POST"])
    def create_rank():
        try:
            data = request.json
            if not data.get("name") or data.get("min_score") is None:
                return jsonify({"success": False, "error": "name e min_score sono obbligatori"}), 400
            rank = Rank(name=data["name"], min_score=int(data["min_score"]))
            db.session.add(rank)
            db.session.commit()
            return jsonify({"success": True, "rank": rank.serialize()})
        except BaseException as e:
            db.session.rollback()
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 400

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/ranks/<int:rank_id>", methods=["PUT"])
    def update_rank(rank_id):
        try:
            rank = db.session.get(Rank, rank_id)
            if not rank:
                return jsonify({"success": False, "error": "Grado non trovato"}), 404
            data = request.json
            if "name" in data:
                rank.name = data["name"]
            if "min_score" in data:
                rank.min_score = int(data["min_score"])
            db.session.commit()
            return jsonify({"success": True, "rank": rank.serialize()})
        except BaseException as e:
            db.session.rollback()
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 400

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/ranks/<int:rank_id>", methods=["DELETE"])
    def delete_rank(rank_id):
        try:
            rank = db.session.get(Rank, rank_id)
            if not rank:
                return jsonify({"success": False, "error": "Grado non trovato"}), 404

            db.session.query(PlayerScore).filter_by(manual_rank_id=rank_id).update({"manual_rank_id": None})
            if rank.badge_filename:
                badge_path = os.path.join(_badges_folder(), rank.badge_filename)
                if os.path.exists(badge_path):
                    os.remove(badge_path)
            db.session.delete(rank)
            db.session.commit()
            return jsonify({"success": True})
        except BaseException as e:
            db.session.rollback()
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 400

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/ranks/<int:rank_id>/badge", methods=["POST"])
    def upload_badge(rank_id):
        try:
            rank = db.session.get(Rank, rank_id)
            if not rank:
                return jsonify({"success": False, "error": "Grado non trovato"}), 404
            if "file" not in request.files:
                return jsonify({"success": False, "error": "Nessun file caricato"}), 400

            upload = request.files["file"]
            extension = pathlib.Path(upload.filename or "").suffix.lower()
            if extension not in ALLOWED_BADGE_EXTENSIONS:
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": f"Estensione non valida, usare una tra {sorted(ALLOWED_BADGE_EXTENSIONS)}",
                        }
                    ),
                    400,
                )

            # Rimuove il badge precedente
            if rank.badge_filename:
                old_path = os.path.join(_badges_folder(), rank.badge_filename)
                if os.path.exists(old_path):
                    os.remove(old_path)

            filename = f"rank_{rank_id}_{uuid.uuid4().hex[:8]}{extension}"
            upload.save(os.path.join(_badges_folder(), filename))
            rank.badge_filename = filename
            db.session.commit()
            return jsonify({"success": True, "rank": rank.serialize()})
        except BaseException as e:
            db.session.rollback()
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 400

    @staticmethod
    @auth_required()
    @blueprint.route("/badges/<file_name>")
    def serve_badge(file_name):
        try:
            folder = _badges_folder()
            if os.path.exists(os.path.join(folder, os.path.basename(file_name))):
                return send_from_directory(folder, os.path.basename(file_name))
            return "", 404
        except BaseException as e:
            logger.error(traceback.format_exc())
            return "", 500

    # ------------------------------------------------------------------
    # Manutenzione tabelle OTS (Alerts / CasEvac)
    # ------------------------------------------------------------------

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/maintenance/stats")
    def maintenance_stats():
        try:
            from opentakserver.models.Alert import Alert
            from opentakserver.models.CasEvac import CasEvac
            from opentakserver.models.Marker import Marker

            return jsonify(
                {
                    "alerts": db.session.query(Alert).count(),
                    "casevac": db.session.query(CasEvac).count(),
                    "markers": db.session.query(Marker).count(),
                }
            )
        except BaseException as e:
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/maintenance/clear", methods=["POST"])
    def maintenance_clear():
        """Svuota la tabella alerts o casevac di OpenTAKServer.

        Eliminazione riga per riga cosi' scattano le cascade ORM (es. ZMIST dei CasEvac).
        """
        try:
            from opentakserver.models.Alert import Alert
            from opentakserver.models.CasEvac import CasEvac
            from opentakserver.models.Marker import Marker

            target = (request.json or {}).get("target")
            models = {"alerts": Alert, "casevac": CasEvac, "markers": Marker}
            if target not in models:
                return jsonify({"success": False, "error": "target deve essere 'alerts', 'casevac' o 'markers'"}), 400

            rows = db.session.query(models[target]).all()
            deleted = len(rows)

            # Per i marker trasmetti anche il CoT di cancellazione agli EUD collegati
            broadcast_ok = True
            if target == "markers" and rows:
                broadcast_ok = _broadcast_marker_deletions(rows)

            for row in rows:
                db.session.delete(row)
            db.session.commit()
            logger.info(f"MilSim maintenance: {current_user.username} deleted {deleted} rows from {target}")

            result = {"success": True, "deleted": deleted}
            if not broadcast_ok:
                result["warning"] = (
                    "Marker eliminati dal database, ma la notifica agli EUD è fallita: "
                    "sugli ATAK collegati potrebbero restare finché non si riconnettono."
                )
            return jsonify(result)
        except BaseException as e:
            db.session.rollback()
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 400

    # ------------------------------------------------------------------
    # Anagrafica giocatori
    # ------------------------------------------------------------------

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/players")
    def get_players():
        try:
            players = (
                db.session.query(Player)
                .order_by(Player.last_name, Player.first_name, Player.callsign)
                .all()
            )
            usernames = {u.id: u.username for u in db.session.query(User).all()}
            results = []
            for player in players:
                data = player.serialize()
                data["username"] = usernames.get(player.user_id)
                results.append(data)
            return jsonify(results)
        except BaseException as e:
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/players", methods=["POST"])
    def create_player():
        try:
            data = request.json or {}
            first_name = (data.get("first_name") or "").strip()
            last_name = (data.get("last_name") or "").strip()
            callsign = (data.get("callsign") or "").strip() or None
            if not (first_name or last_name or callsign):
                return jsonify({"success": False, "error": "Indica almeno nome/cognome o callsign"}), 400

            player = Player(
                first_name=first_name,
                last_name=last_name,
                callsign=callsign,
                notes=data.get("notes"),
                active=data.get("active", True),
            )
            db.session.add(player)
            db.session.commit()
            return jsonify({"success": True, "player": player.serialize()})
        except BaseException as e:
            db.session.rollback()
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 400

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/players/<int:player_id>", methods=["PUT"])
    def update_player(player_id):
        try:
            player = db.session.get(Player, player_id)
            if not player:
                return jsonify({"success": False, "error": "Giocatore non trovato"}), 404

            data = request.json or {}
            for attr in ("first_name", "last_name", "callsign", "notes", "active"):
                if attr in data:
                    setattr(player, attr, data[attr])

            # Associazione account OTS <-> giocatore (user_id null = scollega)
            if "user_id" in data:
                user_id = data["user_id"]
                if user_id is not None:
                    user_id = int(user_id)
                    if not db.session.get(User, user_id):
                        return jsonify({"success": False, "error": "Account OTS non trovato"}), 404
                    already = db.session.query(Player).filter_by(user_id=user_id).first()
                    if already and already.id != player.id:
                        return (
                            jsonify(
                                {
                                    "success": False,
                                    "error": f"Account già associato a {already.display_name()}",
                                }
                            ),
                            400,
                        )
                player.user_id = user_id

            db.session.commit()
            return jsonify({"success": True, "player": player.serialize()})
        except BaseException as e:
            db.session.rollback()
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 400

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/players/<int:player_id>", methods=["DELETE"])
    def delete_player(player_id):
        try:
            player = db.session.get(Player, player_id)
            if not player:
                return jsonify({"success": False, "error": "Giocatore non trovato"}), 404
            if player.attendances:
                return (
                    jsonify(
                        {
                            "success": False,
                            "error": "Il giocatore ha presenze registrate: disattivalo invece di eliminarlo",
                        }
                    ),
                    400,
                )
            db.session.delete(player)
            db.session.commit()
            return jsonify({"success": True})
        except BaseException as e:
            db.session.rollback()
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 400

    # Import giocatori da CSV (export Excel)
    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/players/import/csv", methods=["POST"])
    def import_players_csv():
        """CSV con intestazione: nome,cognome,callsign (o first_name,last_name,callsign).

        Separatore , o ; (qualsiasi export CSV di Excel). Righe duplicate
        (stesso nome+cognome o stesso callsign già in anagrafica) vengono saltate.
        """
        try:
            if "file" not in request.files:
                return jsonify({"success": False, "error": "Nessun file caricato"}), 400

            content = request.files["file"].read().decode("utf-8-sig", errors="replace")
            try:
                dialect = csv.Sniffer().sniff(content.splitlines()[0], delimiters=",;")
            except BaseException:
                dialect = csv.excel

            # Alias di intestazione accettati -> campo interno
            aliases = {
                "nome": "first_name",
                "first_name": "first_name",
                "cognome": "last_name",
                "last_name": "last_name",
                "callsign": "callsign",
                "nickname": "callsign",
                "soprannome": "callsign",
            }

            existing_names = {
                (p.first_name.strip().lower(), p.last_name.strip().lower())
                for p in db.session.query(Player).all()
                if (p.first_name or p.last_name)
            }
            existing_callsigns = {
                p.callsign.strip().lower()
                for p in db.session.query(Player).all()
                if p.callsign
            }

            reader = csv.DictReader(io.StringIO(content), dialect=dialect)
            imported, skipped, errors = 0, 0, []
            for index, line in enumerate(reader, start=2):
                try:
                    row = {}
                    for key, value in line.items():
                        field = aliases.get((key or "").strip().lower())
                        if field:
                            row[field] = (value or "").strip()

                    first_name = row.get("first_name", "")
                    last_name = row.get("last_name", "")
                    callsign = row.get("callsign", "") or None
                    if not (first_name or last_name or callsign):
                        continue  # riga vuota

                    name_key = (first_name.lower(), last_name.lower())
                    if (first_name or last_name) and name_key in existing_names:
                        skipped += 1
                        continue
                    if callsign and callsign.lower() in existing_callsigns:
                        skipped += 1
                        continue

                    db.session.add(
                        Player(first_name=first_name, last_name=last_name, callsign=callsign)
                    )
                    existing_names.add(name_key)
                    if callsign:
                        existing_callsigns.add(callsign.lower())
                    imported += 1
                except BaseException as e:
                    errors.append(f"Riga {index}: {e}")

            db.session.commit()
            logger.info(
                f"MilSim: {current_user.username} imported {imported} players from CSV"
            )
            return jsonify(
                {"success": True, "imported": imported, "skipped": skipped, "errors": errors}
            )
        except BaseException as e:
            db.session.rollback()
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 400

    # Account OTS disponibili per l'associazione a un giocatore
    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/ots-users")
    def ots_users():
        try:
            linked = {p.user_id: p.id for p in db.session.query(Player).all() if p.user_id}
            users = db.session.query(User).order_by(User.username).all()
            return jsonify(
                [
                    {
                        "id": u.id,
                        "username": u.username,
                        "active": u.active,
                        "linked_player_id": linked.get(u.id),
                    }
                    for u in users
                ]
            )
        except BaseException as e:
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    # ------------------------------------------------------------------
    # Classifica e gestione punteggi/gradi giocatore
    # ------------------------------------------------------------------

    @staticmethod
    @auth_required()
    @blueprint.route("/leaderboard")
    def leaderboard():
        try:
            ranks = db.session.query(Rank).order_by(Rank.min_score).all()
            players = (
                db.session.query(Player)
                .filter_by(active=True)
                .order_by(Player.last_name, Player.first_name, Player.callsign)
                .all()
            )
            scores = {s.player_id: s for s in db.session.query(PlayerScore).all()}

            results = []
            for player in players:
                score_row = scores.get(player.id)
                results.append(
                    {
                        "player_id": player.id,
                        "display_name": player.display_name(),
                        "score": score_row.score if score_row else 0,
                        "rank": _resolve_rank(score_row, ranks),
                        "manual_rank_id": score_row.manual_rank_id if score_row else None,
                    }
                )
            results.sort(key=lambda item: item["score"], reverse=True)
            return jsonify(results)
        except BaseException as e:
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/players/<int:player_id>/rank", methods=["POST"])
    def set_player_rank(player_id):
        """Assegna manualmente un grado a un giocatore (rank_id null = torna al calcolo per punteggio)."""
        try:
            if not db.session.get(Player, player_id):
                return jsonify({"success": False, "error": "Giocatore non trovato"}), 404

            rank_id = (request.json or {}).get("rank_id")
            if rank_id is not None and not db.session.get(Rank, int(rank_id)):
                return jsonify({"success": False, "error": "Grado non trovato"}), 404

            score = _get_or_create_score(player_id)
            score.manual_rank_id = int(rank_id) if rank_id is not None else None
            db.session.commit()
            return jsonify({"success": True, "player_score": score.serialize()})
        except BaseException as e:
            db.session.rollback()
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 400

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/players/<int:player_id>/score", methods=["POST"])
    def set_player_score(player_id):
        """Corregge manualmente il punteggio di un giocatore."""
        try:
            if not db.session.get(Player, player_id):
                return jsonify({"success": False, "error": "Giocatore non trovato"}), 404

            value = (request.json or {}).get("score")
            if not isinstance(value, int) or value < 0:
                return jsonify({"success": False, "error": "score deve essere un intero >= 0"}), 400

            score = _get_or_create_score(player_id)
            score.score = value
            db.session.commit()
            return jsonify({"success": True, "player_score": score.serialize()})
        except BaseException as e:
            db.session.rollback()
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 400

    # ------------------------------------------------------------------
    # Anagrafica modalità di gioco
    # ------------------------------------------------------------------

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/modes", methods=["GET"])
    def get_modes():
        return jsonify(serialize_registry())

    # ------------------------------------------------------------------
    # Template di missione
    # ------------------------------------------------------------------

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/templates", methods=["GET"])
    def get_templates():
        try:
            templates = db.session.query(GameTemplate).order_by(GameTemplate.title).all()
            result = []
            for template in templates:
                data = template.serialize()
                # Il Play è possibile solo se il template è completo per la sua modalità
                data["play_errors"] = validate_template(
                    template.mode, data["markers"], data["zones"], for_play=True
                )
                result.append(data)
            return jsonify(result)
        except BaseException as e:
            logger.error(f"MilSim: failed to get templates: {e}")
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/templates", methods=["POST"])
    def create_template():
        try:
            payload, error = _template_payload(request.json or {})
            if error:
                return jsonify({"success": False, "error": error}), 400
            template = GameTemplate(**payload)
            db.session.add(template)
            db.session.commit()
            logger.info(f"MilSim: template '{template.title}' creato da {current_user.username}")
            return jsonify({"success": True, "template": template.serialize()})
        except BaseException as e:
            db.session.rollback()
            logger.error(f"MilSim: failed to create template: {e}")
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/templates/<int:template_id>", methods=["PUT"])
    def update_template(template_id: int):
        try:
            template = db.session.get(GameTemplate, template_id)
            if not template:
                return jsonify({"success": False, "error": "Template non trovato"}), 404
            payload, error = _template_payload(request.json or {})
            if error:
                return jsonify({"success": False, "error": error}), 400
            for key, value in payload.items():
                setattr(template, key, value)
            db.session.commit()
            return jsonify({"success": True, "template": template.serialize()})
        except BaseException as e:
            db.session.rollback()
            logger.error(f"MilSim: failed to update template {template_id}: {e}")
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/templates/<int:template_id>", methods=["DELETE"])
    def delete_template(template_id: int):
        try:
            template = db.session.get(GameTemplate, template_id)
            if not template:
                return jsonify({"success": False, "error": "Template non trovato"}), 404
            # Le partite giocate restano (hanno lo snapshot), sganciate dal template
            for match in db.session.query(GameMatch).filter_by(template_id=template_id).all():
                match.template_id = None
            title = template.title
            db.session.delete(template)
            db.session.commit()
            logger.info(f"MilSim: template '{title}' eliminato da {current_user.username}")
            return jsonify({"success": True})
        except BaseException as e:
            db.session.rollback()
            logger.error(f"MilSim: failed to delete template {template_id}: {e}")
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/templates/<int:template_id>/duplicate", methods=["POST"])
    def duplicate_template(template_id: int):
        try:
            template = db.session.get(GameTemplate, template_id)
            if not template:
                return jsonify({"success": False, "error": "Template non trovato"}), 404
            copy = GameTemplate(
                title=f"{template.title} (copia)",
                description=template.description,
                mode=template.mode,
                duration_minutes=template.duration_minutes,
                map_lat=template.map_lat,
                map_lon=template.map_lon,
                map_zoom=template.map_zoom,
                markers_json=template.markers_json,
                zones_json=template.zones_json,
                packages_json=template.packages_json,
                create_mission=template.create_mission,
            )
            db.session.add(copy)
            db.session.commit()
            return jsonify({"success": True, "template": copy.serialize()})
        except BaseException as e:
            db.session.rollback()
            logger.error(f"MilSim: failed to duplicate template {template_id}: {e}")
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    # ------------------------------------------------------------------
    # Data package disponibili (per l'editor dei template)
    # ------------------------------------------------------------------

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/datapackages", methods=["GET"])
    def get_datapackages():
        try:
            packages = db.session.query(DataPackage).order_by(DataPackage.filename).all()
            return jsonify(
                [
                    {"filename": p.filename, "hash": p.hash, "size": p.size}
                    for p in packages
                ]
            )
        except BaseException as e:
            logger.error(f"MilSim: failed to get data packages: {e}")
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    # ------------------------------------------------------------------
    # Play: dal template alla partita
    # ------------------------------------------------------------------

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/templates/<int:template_id>/play", methods=["POST"])
    def play_template(template_id: int):
        try:
            template = db.session.get(GameTemplate, template_id)
            if not template:
                return jsonify({"success": False, "error": "Template non trovato"}), 404

            snapshot = template.serialize()
            errors = validate_template(template.mode, snapshot["markers"], snapshot["zones"], for_play=True)
            if errors:
                return jsonify({"success": False, "error": "Template incompleto: " + "; ".join(errors)}), 400

            # Destinatari (opzionali) = gruppi TAK di OTS (tabella groups):
            # senza, broadcast a tutti come sempre
            from opentakserver.models.Group import Group

            body = request.json or {}
            team_a_id = body.get("team_a_id") or None
            team_b_id = body.get("team_b_id") or None
            observer_ids = [int(t) for t in (body.get("observer_team_ids") or [])]
            for group_id in filter(None, [team_a_id, team_b_id, *observer_ids]):
                if not db.session.get(Group, int(group_id)):
                    return jsonify({"success": False, "error": f"Gruppo ATAK non trovato: {group_id}"}), 400
            if team_a_id and team_a_id == team_b_id:
                return jsonify({"success": False, "error": "Team A e Team B non possono essere lo stesso gruppo ATAK"}), 400

            # Il Play prepara la missione (stato "ready"): marker, aree e data
            # package vengono pushati subito così le squadre raggiungono gli
            # spawn; il timer parte solo con POST /matches/<id>/start
            match = GameMatch(
                template_id=template.id,
                title=template.title,
                mode=template.mode,
                duration_minutes=template.duration_minutes,
                created_at=_utcnow(),
                status="ready",
                started_by=current_user.username,
                team_a_id=int(team_a_id) if team_a_id else None,
                team_b_id=int(team_b_id) if team_b_id else None,
                observers_json=json.dumps(observer_ids),
                snapshot_json=json.dumps(snapshot),
            )

            # UID stabili per marker e aree (con la loro audience): servono per
            # ripubblicare e cancellare presso gli stessi destinatari
            run = uuid.uuid4().hex[:10]
            uids = []
            for i, marker in enumerate(snapshot["markers"]):
                mtype = MARKER_TYPES[marker["type"]]
                uids.append({"uid": f"GM.{run}.m{i}", "cot_type": mtype["cot_type"],
                             "audience": mtype.get("audience", "all")})
            for j, zone in enumerate(snapshot["zones"]):
                ztype = ZONE_TYPES[zone["type"]]
                uids.append({"uid": f"GM.{run}.m{len(snapshot['markers']) + j}", "cot_type": "u-d-f",
                             "audience": ztype.get("audience", "all")})
            match.cot_uids_json = json.dumps(uids)

            targets = engine.resolve_targets(match)
            if targets is not None and not targets["all"]:
                return jsonify(
                    {"success": False, "error": "I team selezionati non hanno nessun EUD: i giocatori devono impostare il colore squadra su ATAK"}
                ), 400
            items = _match_items(match, uids, targets)

            package_events, missing = _package_events(snapshot.get("packages", []))
            all_targets = engine.audience_targets(targets, "all")
            items.extend((event, all_targets) for event in package_events)

            mode_name = GAME_MODES[template.mode]["name"]

            # Flag "crea missione" sul template: nasce una missione Data Sync
            # collegata alla partita, così l'admin può definirne i dataset e
            # assegnarla ai team con 🎯 Assegna missione
            if template.create_mission:
                from opentakserver.models.Mission import Mission

                data_sync_name = skyfi.safe_name(f"{template.title}-{run[:6]}")
                mission = Mission()
                mission.name = data_sync_name
                mission.guid = str(uuid.uuid4())
                mission.tool = "public"
                # creator_uid è FK verso euds.uid: il Game Master non è un EUD
                mission.creator_uid = None
                mission.create_time = _utcnow()
                mission.description = f"Partita {template.title} ({mode_name}) — MilSim Companion"
                mission.group = "__ANON__"
                mission.default_role = "MISSION_SUBSCRIBER"
                mission.password_protected = False
                db.session.add(mission)

                mission_change = MissionChange()
                mission_change.isFederatedChange = False
                mission_change.change_type = MissionChange.CREATE_MISSION
                mission_change.mission_name = data_sync_name
                mission_change.timestamp = datetime.now(timezone.utc)
                mission_change.creator_uid = current_user.username
                mission_change.server_time = datetime.now(timezone.utc)
                db.session.add(mission_change)

                match.mission_name = data_sync_name
                items.append(
                    (cot.mission_announce_event(data_sync_name, mission.guid, "public", _gm_sender_uid()), all_targets)
                )

            chat = (
                f"🎮 Missione pronta: {template.title} ({mode_name}, {template.duration_minutes} minuti). "
                f"Raggiungete gli spawn e attendete la luce verde."
            )
            if template.description:
                chat += f" {template.description}"
            items.append((cot.geochat_event(chat, _gm_sender_uid(), _gm_callsign()), all_targets))

            if not cot.deliver(items):
                db.session.rollback()
                return jsonify(
                    {"success": False, "error": "Push agli EUD fallito (RabbitMQ non raggiungibile): partita non creata"}
                ), 502

            db.session.add(match)
            db.session.commit()

            logger.info(
                f"MilSim: missione '{match.title}' ({mode_name}) preparata da {current_user.username}: "
                f"{len(snapshot['markers'])} marker, {len(snapshot['zones'])} aree, "
                f"{len(package_events)} data package annunciati"
            )
            result = {"success": True, "match": match.serialize()}
            if missing:
                result["warning"] = f"{len(missing)} data package del template non esistono più sul server"
            return jsonify(result)
        except BaseException as e:
            db.session.rollback()
            logger.error(f"MilSim: failed to play template {template_id}: {e}")
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    # ------------------------------------------------------------------
    # Partite
    # ------------------------------------------------------------------

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/matches", methods=["GET"])
    def get_matches():
        try:
            matches = db.session.query(GameMatch).order_by(GameMatch.created_at.desc()).limit(100).all()
            from opentakserver.models.Team import Team

            team_names = {t.id: t.name for t in db.session.query(Team).all()}
            results = []
            for match in matches:
                data = match.serialize()
                data["groups"] = {
                    "team_a": team_names.get(match.team_a_id),
                    "team_b": team_names.get(match.team_b_id),
                    "observers": [team_names[t] for t in data["observer_team_ids"] if t in team_names],
                }
                results.append(data)
            return jsonify(results)
        except BaseException as e:
            logger.error(f"MilSim: failed to get matches: {e}")
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    # ------------------------------------------------------------------
    # Gruppi ATAK (tabella groups di OTS: i gruppi/canali TAK del server)
    # ------------------------------------------------------------------

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/groups", methods=["GET"])
    def get_groups():
        """Gruppi TAK definiti sul server (tabella `groups` di OTS, gestiti
        dalla pagina Groups della web UI / API di OTS), con gli EUD dei loro
        utenti: sono i destinatari selezionabili come Team A/B/osservatori.
        Il plugin non gestisce i gruppi: li legge soltanto."""
        try:
            from opentakserver.models.EUD import EUD
            from opentakserver.models.Group import Group
            from opentakserver.models.GroupUser import GroupUser

            # EUD per utente (un utente può avere più dispositivi)
            euds_by_user: dict[int, list] = {}
            for eud in db.session.query(EUD).order_by(EUD.callsign).all():
                if eud.user_id:
                    euds_by_user.setdefault(eud.user_id, []).append(
                        {
                            "uid": eud.uid,
                            "callsign": eud.callsign,
                            "last_event_time": eud.last_event_time.isoformat() + "Z" if eud.last_event_time else None,
                        }
                    )

            usernames = {u.id: u.username for u in db.session.query(User).all()}

            # Utenti (abilitati) per gruppo, qualunque direzione IN/OUT
            users_by_group: dict[int, set] = {}
            for membership in db.session.query(GroupUser).filter_by(enabled=True).all():
                users_by_group.setdefault(membership.group_id, set()).add(membership.user_id)

            result = []
            for group in db.session.query(Group).order_by(Group.name).all():
                members = []
                for user_id in sorted(users_by_group.get(group.id, set())):
                    user_euds = euds_by_user.get(user_id, [])
                    for eud in user_euds:
                        members.append({**eud, "username": usernames.get(user_id)})
                    if not user_euds:
                        # Utente nel gruppo ma senza EUD registrati: mostralo comunque
                        members.append({"uid": None, "callsign": None, "username": usernames.get(user_id), "last_event_time": None})
                result.append(
                    {
                        "id": group.id,
                        "name": group.name,
                        "description": group.description,
                        "members": members,
                        "eud_count": sum(1 for m in members if m["uid"]),
                    }
                )
            return jsonify(result)
        except BaseException as e:
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/matches/<int:match_id>/start", methods=["POST"])
    def start_match(match_id: int):
        """Luce verde: fissa inizio/fine, ripubblica i marker con lo stale vero
        (stessi UID) e annuncia la partenza; da qui il match engine tiene il
        tempo e chiude la partita da solo allo scadere."""
        try:
            match = db.session.get(GameMatch, match_id)
            if not match:
                return jsonify({"success": False, "error": "Partita non trovata"}), 404
            if match.status != "ready":
                return jsonify({"success": False, "error": "La partita è già iniziata o terminata"}), 400

            now = _utcnow()
            match.started_at = now
            match.ends_at = now + timedelta(minutes=match.duration_minutes)
            match.status = "running"

            targets = engine.resolve_targets(match)
            uids = json.loads(match.cot_uids_json)
            items = _match_items(match, uids, targets)
            mode_name = GAME_MODES.get(match.mode, {}).get("name", match.mode)
            fine = match.ends_at.strftime("%H:%M")
            items.append(
                (
                    cot.geochat_event(
                        f"🟢 LUCE VERDE — la partita {match.title} ({mode_name}) è INIZIATA! "
                        f"Durata {match.duration_minutes} minuti, fine alle {fine} UTC.",
                        _gm_sender_uid(),
                        _gm_callsign(),
                    ),
                    engine.audience_targets(targets, "all"),
                )
            )
            if not cot.deliver(items):
                db.session.rollback()
                return jsonify(
                    {"success": False, "error": "Push agli EUD fallito (RabbitMQ non raggiungibile): partita non avviata"}
                ), 502

            db.session.commit()
            logger.info(f"MilSim: luce verde su '{match.title}' da {current_user.username}, fine {fine} UTC")
            return jsonify({"success": True, "match": match.serialize()})
        except BaseException as e:
            db.session.rollback()
            logger.error(f"MilSim: failed to start match {match_id}: {e}")
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/matches/<int:match_id>/invite", methods=["POST"])
    def invite_mission(match_id: int):
        """🎯 Assegna missione: manda l'invito alla missione Data Sync della
        partita agli EUD dei team coinvolti (t-x-m-i con token, come gli
        inviti Marti di OTS): su ATAK compare la richiesta di iscrizione."""
        try:
            match = db.session.get(GameMatch, match_id)
            if not match:
                return jsonify({"success": False, "error": "Partita non trovata"}), 404
            if match.status not in ("ready", "running"):
                return jsonify({"success": False, "error": "La partita è già terminata"}), 400
            if not match.mission_name:
                return jsonify({"success": False, "error": "La partita non ha una missione (flag «Crea missione» nel template)"}), 400

            from opentakserver.blueprints.marti_api.mission_marti_api import generate_token
            from opentakserver.models.Mission import Mission

            mission = db.session.execute(
                db.session.query(Mission).filter_by(name=match.mission_name)
            ).scalar()
            if not mission:
                return jsonify({"success": False, "error": f"Missione non trovata sul server: {match.mission_name}"}), 404

            targets = engine.resolve_targets(match)
            if targets is None or not targets["all"]:
                return jsonify(
                    {"success": False, "error": "Partita senza gruppi (broadcast): senza Team A/B non so a chi assegnare la missione"}
                ), 400

            items = []
            for eud_uid in sorted(targets["all"]):
                token = generate_token(mission, eud_uid)
                items.append(
                    (
                        cot.mission_invite_event(
                            mission.name, mission.guid, mission.tool or "public", _gm_sender_uid(), token
                        ),
                        {eud_uid},
                    )
                )
            if not cot.deliver(items):
                return jsonify({"success": False, "error": "Invio inviti fallito (RabbitMQ non raggiungibile)"}), 502

            logger.info(
                f"MilSim: missione '{mission.name}' assegnata a {len(items)} EUD della partita "
                f"'{match.title}' da {current_user.username}"
            )
            return jsonify({"success": True, "mission": mission.name, "invited": len(items)})
        except BaseException as e:
            logger.error(f"MilSim: failed to invite mission for match {match_id}: {e}")
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/matches/<int:match_id>/event", methods=["POST"])
    def match_event(match_id: int):
        """Evento di partita per l'arbitro della modalità (es. Bomb Defusal:
        bomb_planted / bomb_defused / bomb_exploded). Oggi lo preme il Game
        Master dalla UI, domani lo chiamerà l'orchestratore in campo: se
        l'evento decreta la vittoria la partita finisce prima del tempo."""
        try:
            match = db.session.get(GameMatch, match_id)
            if not match:
                return jsonify({"success": False, "error": "Partita non trovata"}), 404
            if match.status != "running":
                return jsonify({"success": False, "error": "La partita non è in corso"}), 400

            event_key = (request.json or {}).get("event")
            events = engine.match_events_for(match.mode)
            spec = events.get(event_key)
            if not spec:
                valid = ", ".join(events) or "nessuno per questa modalità"
                return jsonify({"success": False, "error": f"Evento non valido: {event_key} (validi: {valid})"}), 400

            if spec["ends"]:
                chat = f"{spec['chat']} Partita terminata: {match.title}."
                broadcast_ok = engine.finish_match(match, "objective", spec["winner"], chat)
                logger.info(
                    f"MilSim: partita '{match.title}' chiusa per obiettivo ({event_key}) da {current_user.username}"
                )
            else:
                targets = engine.resolve_targets(match)
                broadcast_ok = cot.deliver(
                    [(cot.geochat_event(spec["chat"], _gm_sender_uid(), _gm_callsign()),
                      engine.audience_targets(targets, "all"))]
                )
                logger.info(f"MilSim: evento {event_key} su '{match.title}' da {current_user.username}")

            result = {"success": True, "ended": spec["ends"], "match": match.serialize()}
            if not broadcast_ok:
                result["warning"] = "Annuncio agli EUD fallito (RabbitMQ non raggiungibile)"
            return jsonify(result)
        except BaseException as e:
            db.session.rollback()
            logger.error(f"MilSim: failed match event on {match_id}: {e}")
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/matches/<int:match_id>/republish", methods=["POST"])
    def republish_match(match_id: int):
        """Ripubblica marker e aree con gli stessi UID: per gli EUD entrati a
        partita in corso (il broadcast iniziale raggiunge solo i connessi)."""
        try:
            match = db.session.get(GameMatch, match_id)
            if not match:
                return jsonify({"success": False, "error": "Partita non trovata"}), 404
            if match.status not in ("ready", "running"):
                return jsonify({"success": False, "error": "La partita è già terminata"}), 400
            if match.ends_at and match.ends_at <= _utcnow():
                return jsonify({"success": False, "error": "La partita è scaduta: i marker non vengono ripubblicati"}), 400

            uids = json.loads(match.cot_uids_json)
            targets = engine.resolve_targets(match)
            if not cot.deliver(_match_items(match, uids, targets)):
                return jsonify({"success": False, "error": "Push agli EUD fallito (RabbitMQ non raggiungibile)"}), 502
            return jsonify({"success": True, "republished": len(uids)})
        except BaseException as e:
            logger.error(f"MilSim: failed to republish match {match_id}: {e}")
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/matches/<int:match_id>/end", methods=["POST"])
    def end_match(match_id: int):
        """Chiusura manuale dal GM: annulla una partita pronta o termina una
        partita in corso (il fine-tempo automatico lo gestisce il match engine)."""
        try:
            match = db.session.get(GameMatch, match_id)
            if not match:
                return jsonify({"success": False, "error": "Partita non trovata"}), 404
            if match.status not in ("ready", "running"):
                return jsonify({"success": False, "error": "La partita è già terminata"}), 400

            if match.status == "ready":
                chat = f"🚫 Missione annullata: {match.title}."
            else:
                chat = f"🏁 Partita terminata dal Game Master: {match.title}."
            broadcast_ok = engine.finish_match(match, "manual", None, chat)

            logger.info(f"MilSim: partita '{match.title}' chiusa manualmente da {current_user.username}")
            result = {"success": True, "match": match.serialize()}
            if not broadcast_ok:
                result["warning"] = (
                    "Partita chiusa, ma la cancellazione dei marker sugli EUD è fallita: "
                    "spariranno comunque da soli allo scadere dello stale."
                )
            return jsonify(result)
        except BaseException as e:
            db.session.rollback()
            logger.error(f"MilSim: failed to end match {match_id}: {e}")
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/matches/<int:match_id>", methods=["DELETE"])
    def delete_match(match_id: int):
        """Elimina una sessione dallo storico. Solo per partite terminate:
        quelle pronte/in corso vanno prima chiuse (Annulla/Termina), che si
        occupa anche di cancellare i marker dagli EUD."""
        try:
            match = db.session.get(GameMatch, match_id)
            if not match:
                return jsonify({"success": False, "error": "Sessione non trovata"}), 404
            if match.status != "ended":
                return jsonify(
                    {"success": False, "error": "La sessione non è terminata: usa prima Annulla/Termina"}
                ), 400
            title = match.title
            db.session.delete(match)
            db.session.commit()
            logger.info(f"MilSim: sessione '{title}' eliminata dallo storico da {current_user.username}")
            return jsonify({"success": True})
        except BaseException as e:
            db.session.rollback()
            logger.error(f"MilSim: failed to delete match {match_id}: {e}")
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/matches/<int:match_id>/replay")
    def match_replay(match_id: int):
        """Replay della partita: stesse tracce GPS del replay evento, ma la
        finestra è esattamente started_at → ended_at tenuti dal server (già in
        UTC come i punti CoT: nessuna conversione di fuso)."""
        try:
            match = db.session.get(GameMatch, match_id)
            if not match:
                return jsonify({"success": False, "error": "Partita non trovata"}), 404
            if not match.started_at:
                return jsonify({"success": False, "error": "La partita non è mai stata avviata: nessuna finestra da rigiocare"}), 400

            try:
                step = max(0, int(request.args.get("step", 5)))
            except ValueError:
                step = 5

            start_utc = match.started_at.replace(tzinfo=timezone.utc)
            end_utc = (match.ended_at or match.ends_at or _utcnow()).replace(tzinfo=timezone.utc)

            return jsonify(
                {
                    "match": match.serialize(),
                    "start": start_utc.timestamp(),
                    "end": end_utc.timestamp(),
                    "step": step,
                    "tracks": _gps_tracks(start_utc, end_utc, step),
                }
            )
        except BaseException as e:
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    # ------------------------------------------------------------------
    # Ordini SkyFi (ereditato dal fork OTS-SkyFi-Plugin)
    # ------------------------------------------------------------------

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/orders", methods=["GET"])
    def get_orders():
        try:
            params = {
                "pageNumber": request.args.get("page", 0),
                "pageSize": request.args.get("page_size", 9),
            }
            if request.args.get("search"):
                params["search"] = request.args.get("search")

            r = requests.get(f"{skyfi.BASE_URL}/orders", headers=skyfi.headers(), params=params, timeout=30)
            if r.status_code == 200:
                data = r.json()
                # Rimozione logica: gli ordini nascosti spariscono dalla lista
                # (su SkyFi non si possono cancellare). Con show_hidden=1 si
                # mostrano tutti, marcati con _hidden per il ripristino da UI.
                hidden = {h.order_uid for h in db.session.query(SkyfiHiddenOrder).all()}
                if hidden:
                    orders = data.get("orders") or []
                    if request.args.get("show_hidden"):
                        for o in orders:
                            o["_hidden"] = (o.get("id") or o.get("orderId")) in hidden
                    else:
                        data["orders"] = [o for o in orders if (o.get("id") or o.get("orderId")) not in hidden]
                data["hidden_total"] = len(hidden)
                return jsonify(data)

            logger.error(f"Failed to get orders: {r.text}")
            return jsonify({"success": False, "error": "Controlla l'API key SkyFi e riprova"}), 400
        except BaseException as e:
            logger.error(f"Failed to get orders: {e}")
            return jsonify({"success": False, "error": f"Failed to get orders: {str(e)}"}), 400

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/orders/<uid>", methods=["GET"])
    def get_order(uid: str):
        try:
            r = requests.get(f"{skyfi.BASE_URL}/orders/{uid}", headers=skyfi.headers(), timeout=30)
            if r.status_code == 200:
                return jsonify(r.json())
            return jsonify({"success": False, "error": f"Ordine non trovato: {r.status_code}"}), r.status_code
        except BaseException as e:
            logger.error(f"Failed to get order {uid}: {e}")
            return jsonify({"success": False, "error": str(e)}), 400

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/orders/<uid>/hide", methods=["POST", "DELETE"])
    def hide_order(uid: str):
        """POST nasconde l'ordine dalla lista (rimozione logica), DELETE lo ripristina."""
        try:
            row = db.session.execute(
                db.session.query(SkyfiHiddenOrder).filter_by(order_uid=uid)
            ).scalar()
            if request.method == "DELETE":
                if row:
                    db.session.delete(row)
                    db.session.commit()
                return jsonify({"success": True, "hidden": False}), 200
            if not row:
                row = SkyfiHiddenOrder(
                    order_uid=uid,
                    order_code=(request.json or {}).get("order_code") if request.is_json else None,
                    hidden_by=current_user.username,
                )
                db.session.add(row)
                db.session.commit()
            return jsonify({"success": True, "hidden": True}), 200
        except BaseException as e:
            db.session.rollback()
            logger.error(f"MilSim/SkyFi: hide/unhide ordine {uid} fallito: {e}")
            return jsonify({"success": False, "error": str(e)}), 500

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/orders/<uid>/image")
    def get_preview_image(uid: str):
        r = requests.get(f"{skyfi.BASE_URL}/orders/{uid}/image", headers=skyfi.headers(), timeout=60)
        if r.status_code == 200:
            return f"data:image/png;base64,{base64.b64encode(r.content).decode('UTF-8')}", 200
        return jsonify({"success": False, "error": f"Image download failed with status code {r.status_code}"}), r.status_code

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/orders/<uid>/download/<deliverable_type>", methods=["GET"])
    def download_deliverable(uid: str, deliverable_type: str):
        """Scarica un deliverable (image/payload/cog/view-ready) facendo da proxy
        verso l'URL firmato di SkyFi, così l'API key non arriva mai al browser."""
        if deliverable_type not in skyfi.DELIVERABLE_TYPES:
            return jsonify({"success": False, "error": f"Tipo non valido: {deliverable_type}"}), 400

        try:
            order = skyfi.get_order(uid) or {}
            r = requests.get(
                f"{skyfi.BASE_URL}/orders/{uid}/{deliverable_type}",
                headers=skyfi.headers(),
                stream=True,
                allow_redirects=True,
                timeout=(10, 300),
            )
            if r.status_code != 200:
                logger.error(f"Deliverable {deliverable_type} for {uid} failed: {r.status_code}")
                return jsonify({"success": False, "error": f"Download fallito: HTTP {r.status_code}"}), r.status_code

            filename = skyfi.deliverable_filename(r, order, uid, deliverable_type)

            headers = {
                "Content-Type": r.headers.get("Content-Type", "application/octet-stream"),
                "Content-Disposition": f'attachment; filename="{filename}"',
            }
            if r.headers.get("Content-Length"):
                headers["Content-Length"] = r.headers["Content-Length"]

            return Response(
                stream_with_context(r.iter_content(chunk_size=64 * 1024)),
                status=200,
                headers=headers,
            )
        except BaseException as e:
            logger.error(f"Failed to download {deliverable_type} for {uid}: {e}")
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/orders/<uid>/data_package", methods=["POST", "GET"])
    def create_skyfi_data_package(uid: str):
        try:
            order = skyfi.get_order(uid)
            if not order:
                return jsonify({"success": False, "error": "Ordine non trovato su SkyFi"}), 404
            if not order.get("tilesUrl"):
                return jsonify({"success": False, "error": "L'ordine non ha ancora i tile WMTS (tilesUrl)"}), 400

            location = order.get("geocodeLocation") or order.get("label") or ""
            package_name = skyfi.safe_name(f"SkyFi-{order['orderCode']}_{location}")

            multi_layer_tile_source = ET.Element("customMultiLayerMapSource")
            # ATAK esige il nome come elemento <name>: come testo della radice
            # (bug ereditato dall'upstream) l'import del layer fallisce con
            # RuntimeException e l'XML resta nel pacchetto come file generico
            ET.SubElement(multi_layer_tile_source, "name").text = f"SkyFi-{order['orderCode']} {location}"

            layers = ET.SubElement(multi_layer_tile_source, "layers")

            google_tiles = ET.SubElement(layers, "customMapSource")
            ET.SubElement(google_tiles, "name").text = "Google Hybrid"
            ET.SubElement(google_tiles, "minZoom").text = "0"
            ET.SubElement(google_tiles, "maxZoom").text = "22"
            ET.SubElement(google_tiles, "tileType").text = "jpg"
            ET.SubElement(google_tiles, "tileUpdate").text = "None"
            # & letterale: è ElementTree a fare l'escape in serializzazione
            # (l'unquote upstream non decodeva le entity e produceva &amp;amp;)
            ET.SubElement(google_tiles, "url").text = "http://mt1.google.com/vt/lyrs=y&x={$x}&y={$y}&z={$z}"

            skyfi_tiles = ET.SubElement(layers, "customMapSource")
            ET.SubElement(skyfi_tiles, "name").text = f"SkyFi-{order['orderCode']} {location}"
            ET.SubElement(skyfi_tiles, "minZoom").text = "0"
            ET.SubElement(skyfi_tiles, "maxZoom").text = "22"
            ET.SubElement(skyfi_tiles, "tileType").text = "png"
            ET.SubElement(skyfi_tiles, "tileUpdate").text = "None"
            ET.SubElement(skyfi_tiles, "url").text = unquote(
                order["tilesUrl"].replace("{z}", "{$z}").replace("{x}", "{$x}").replace("{y}", "{$y}")
            )

            xml_path = os.path.join(app.config.get("UPLOAD_FOLDER"), f"{package_name}.xml")
            with open(xml_path, "w") as f:
                f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
                f.write(ET.tostring(multi_layer_tile_source).decode("UTF-8"))

            data_package_hash = create_data_package_zip(xml_path)

            try:
                os.remove(xml_path)
            except OSError:
                pass

            # save_data_package_to_db di OTS può lasciare submission_user nullo:
            # registra esplicitamente l'utente corrente come mittente. La build
            # ATAK in uso mostra come "User" il CreatorUid (null → scritta
            # "null"): è una FK verso euds.uid (lo username fa fallire il
            # commit), quindi ci va l'ultimo dispositivo dell'utente corrente
            try:
                from opentakserver.models.EUD import EUD

                data_package = db.session.execute(
                    db.session.query(DataPackage).filter_by(hash=data_package_hash)
                ).scalar()
                if data_package:
                    data_package.submission_user = current_user.id
                    eud = (
                        db.session.query(EUD)
                        .filter_by(user_id=current_user.id)
                        .order_by(EUD.last_event_time.desc().nulls_last())
                        .first()
                    )
                    if eud:
                        data_package.creator_uid = eud.uid
                    db.session.commit()
            except BaseException as e:
                db.session.rollback()
                logger.warning(f"MilSim/SkyFi: mittente non registrato sul data package {package_name}: {e}")

            return jsonify({"success": True, "name": package_name, "hash": data_package_hash}), 200
        except BaseException as e:
            logger.error(f"Failed to create data package for {uid}: {e}")
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    # ------------------------------------------------------------------
    # Stato mappe PCN (Geoportale Italia)
    # ------------------------------------------------------------------

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/pcn/status", methods=["GET"])
    def pcn_status():
        """Verifica se il WMS del PCN (le mappe IGM/ortofoto dei data package
        del gruppo) sta servendo davvero le tile: quando è giù, su ATAK/WinTAK
        la mappa resta verde/vuota senza alcun messaggio d'errore."""
        try:
            with ThreadPoolExecutor(max_workers=len(skyfi.PCN_SERVICES)) as pool:
                services = list(pool.map(skyfi.check_pcn_service, skyfi.PCN_SERVICES))
            online = sum(1 for s in services if s["status"] == "online")
            status = "online" if online == len(services) else ("offline" if online == 0 else "degradato")
            return jsonify({
                "status": status,
                "online": online,
                "total": len(services),
                "services": services,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            })
        except BaseException as e:
            logger.error(f"PCN status check failed: {e}")
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    # ------------------------------------------------------------------
    # Missioni (Data Sync)
    # ------------------------------------------------------------------

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/missions", methods=["GET"])
    def get_missions():
        try:
            missions = db.session.execute(db.session.query(Mission)).scalars().all()
            content_counts = dict(
                db.session.execute(
                    db.session.query(MissionContentMission.mission_name, db.func.count())
                    .group_by(MissionContentMission.mission_name)
                ).all()
            )
            return jsonify(
                [
                    {
                        "name": m.name,
                        "guid": m.guid,
                        "description": m.description,
                        "password_protected": bool(m.password_protected),
                        "content_count": content_counts.get(m.name, 0),
                    }
                    for m in missions
                ]
            )
        except BaseException as e:
            logger.error(f"Failed to get missions: {e}")
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/missions/<mission_name>/contents", methods=["GET"])
    def get_mission_contents(mission_name: str):
        """Elenca i contenuti (dataset) condivisi su una missione Data Sync,
        qualunque sia la fonte (questo plugin, ATAK, web UI): la web UI di OTS
        non li mostra, quindi questa è la vista amministrativa per gestirli."""
        try:
            mission = db.session.execute(
                db.session.query(Mission).filter_by(name=mission_name)
            ).scalar()
            if not mission:
                return jsonify({"success": False, "error": f"Missione non trovata: {mission_name}"}), 404

            contents = (
                db.session.execute(
                    db.session.query(MissionContent)
                    .join(MissionContentMission, MissionContentMission.mission_content_id == MissionContent.id)
                    .filter(MissionContentMission.mission_name == mission_name)
                    .order_by(MissionContent.submission_time.desc())
                )
                .scalars()
                .all()
            )
            return jsonify(
                [
                    {
                        "filename": c.filename,
                        "hash": c.hash,
                        "uid": c.uid,
                        "size": c.size,
                        "mime_type": c.mime_type,
                        "submitter": c.submitter,
                        "submission_time": c.submission_time.isoformat() if c.submission_time else None,
                        "keywords": c.keywords or [],
                        "on_disk": skyfi.mission_content_location(c) is not None,
                    }
                    for c in contents
                ]
            )
        except BaseException as e:
            logger.error(f"Failed to get contents for mission {mission_name}: {e}")
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/missions/<mission_name>/contents/<file_hash>/download", methods=["GET"])
    def download_mission_content(mission_name: str, file_hash: str):
        """Scarica dal browser un contenuto missione, cercando il file negli
        stessi posti di /Marti/sync/content (che però è raggiungibile solo
        dagli EUD con certificato, non dalla web UI)."""
        try:
            content = db.session.execute(
                db.session.query(MissionContent).filter_by(hash=file_hash)
            ).scalar()
            if not content:
                return jsonify({"success": False, "error": f"Nessun contenuto con hash {file_hash}"}), 404

            location = skyfi.mission_content_location(content)
            if not location:
                return jsonify({"success": False, "error": f"File non trovato sul server: {content.filename}"}), 404

            folder, name = location
            return send_from_directory(folder, name, as_attachment=True, download_name=content.filename)
        except BaseException as e:
            logger.error(f"Failed to download mission content {file_hash}: {e}")
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/missions/<mission_name>/contents/<file_hash>/preview", methods=["GET"])
    def preview_mission_content(mission_name: str, file_hash: str):
        """Serve inline (non come download) un contenuto immagine, per le
        anteprime nella tabella del tab Missioni."""
        try:
            content = db.session.execute(
                db.session.query(MissionContent).filter_by(hash=file_hash)
            ).scalar()
            if not content:
                return jsonify({"success": False, "error": f"Nessun contenuto con hash {file_hash}"}), 404

            mime = (content.mime_type or "").lower()
            if not mime.startswith("image/"):
                guessed, _ = mimetypes.guess_type(content.filename or "")
                if guessed and guessed.startswith("image/"):
                    mime = guessed
                else:
                    return jsonify({"success": False, "error": "Anteprima disponibile solo per le immagini"}), 415

            location = skyfi.mission_content_location(content)
            if not location:
                return jsonify({"success": False, "error": f"File non trovato sul server: {content.filename}"}), 404

            folder, name = location
            return send_from_directory(folder, name, as_attachment=False, mimetype=mime, max_age=3600)
        except BaseException as e:
            logger.error(f"Failed to preview mission content {file_hash}: {e}")
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/missions/<mission_name>/contents/<file_hash>", methods=["DELETE"])
    def remove_mission_content(mission_name: str, file_hash: str):
        """Rimuove un contenuto dalla missione replicando il flusso di
        DELETE /Marti/api/missions/<name>/contents: si cancella solo il link
        contenuto↔missione e si registra un MissionChange REMOVE_CONTENT; il
        file resta su disco e nel DB così lo storico della missione rimane
        corretto e il contenuto può essere riassegnato."""
        try:
            mission = db.session.execute(
                db.session.query(Mission).filter_by(name=mission_name)
            ).scalar()
            if not mission:
                return jsonify({"success": False, "error": f"Missione non trovata: {mission_name}"}), 404

            content = db.session.execute(
                db.session.query(MissionContent).filter_by(hash=file_hash)
            ).scalar()
            if not content:
                return jsonify({"success": False, "error": f"Nessun contenuto con hash {file_hash}"}), 404

            mission_content_mission = db.session.execute(
                db.session.query(MissionContentMission).filter_by(
                    mission_name=mission_name, mission_content_id=content.id
                )
            ).scalar()
            if not mission_content_mission:
                return jsonify(
                    {"success": False, "error": f"Il contenuto non è assegnato alla missione {mission_name}"}
                ), 404

            username = current_user.username if current_user else "MilSim-Plugin"

            db.session.delete(mission_content_mission)

            mission_change = MissionChange()
            mission_change.isFederatedChange = False
            mission_change.change_type = MissionChange.REMOVE_CONTENT
            mission_change.content_uid = content.uid
            mission_change.mission_name = mission_name
            mission_change.timestamp = datetime.now(timezone.utc)
            mission_change.creator_uid = username
            mission_change.server_time = datetime.now(timezone.utc)
            db.session.add(mission_change)
            db.session.commit()

            skyfi.notify_mission_change(mission_name, mission, mission_change, content)

            logger.info(f"MilSim/SkyFi: {content.filename} rimosso dalla missione {mission_name} da {username}")
            return jsonify({"success": True, "filename": content.filename, "mission": mission_name})
        except BaseException as e:
            logger.error(f"Failed to remove content {file_hash} from mission {mission_name}: {e}")
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/orders/<uid>/mission", methods=["POST"])
    def assign_to_mission(uid: str):
        """Scarica un deliverable da SkyFi e lo aggiunge come contenuto di una
        missione Data Sync, replicando il flusso di /Marti/sync/upload +
        PUT /Marti/api/missions/<name>/contents: gli EUD iscritti ricevono il
        CoT di mission change e scaricano il file da /Marti/sync/content."""
        body = request.json or {}
        mission_name = body.get("mission")
        deliverable_type = body.get("deliverable_type", "payload")

        if not mission_name:
            return jsonify({"success": False, "error": "Manca il nome della missione"}), 400
        if deliverable_type not in skyfi.DELIVERABLE_TYPES:
            return jsonify({"success": False, "error": f"Tipo non valido: {deliverable_type}"}), 400

        try:
            mission = db.session.execute(
                db.session.query(Mission).filter_by(name=mission_name)
            ).scalar()
            if not mission:
                return jsonify({"success": False, "error": f"Missione non trovata: {mission_name}"}), 404

            order = skyfi.get_order(uid)
            if not order:
                return jsonify({"success": False, "error": "Ordine non trovato su SkyFi"}), 404

            # Download in streaming nella cartella missioni, con hash calcolato al volo
            missions_folder = os.path.join(app.config.get("OTS_DATA_FOLDER"), "missions")
            os.makedirs(missions_folder, exist_ok=True)

            r = requests.get(
                f"{skyfi.BASE_URL}/orders/{uid}/{deliverable_type}",
                headers=skyfi.headers(),
                stream=True,
                allow_redirects=True,
                timeout=(10, 600),
            )
            if r.status_code != 200:
                return jsonify({"success": False, "error": f"Download da SkyFi fallito: HTTP {r.status_code}"}), r.status_code

            filename = skyfi.deliverable_filename(r, order, uid, deliverable_type)
            mime_type = r.headers.get("Content-Type", "application/octet-stream").split(";")[0]

            sha256 = hashlib.sha256()
            size = 0
            tmp_path = os.path.join(missions_folder, f".skyfi-{uuid.uuid4().hex}.part")
            try:
                with open(tmp_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        sha256.update(chunk)
                        size += len(chunk)
                        f.write(chunk)
                file_hash = sha256.hexdigest()
                # /Marti/sync/content serve i contenuti missione per nome file
                os.replace(tmp_path, os.path.join(missions_folder, filename))
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

            username = current_user.username if current_user else "MilSim-Plugin"

            # Stesso flusso di /Marti/sync/upload: MissionContent riusato se lo
            # stesso file (hash) è già presente
            content = db.session.execute(
                db.session.query(MissionContent).filter_by(hash=file_hash)
            ).scalar()
            if not content:
                content = MissionContent()
                content.mime_type = mime_type
                content.filename = filename
                content.submission_time = datetime.now(timezone.utc)
                content.submitter = username
                content.uid = str(uuid.uuid4())
                content.creator_uid = username
                content.size = size
                content.expiration = -1
                content.keywords = ["skyfi", order.get("orderCode", uid)]
                content.hash = file_hash
                db.session.execute(insert(MissionContent).values(**content.serialize()))
                db.session.commit()
                content = db.session.execute(
                    db.session.query(MissionContent).filter_by(hash=file_hash)
                ).scalar()
            elif content.filename != filename:
                # Il file su disco è stato salvato col nuovo nome: allinea il DB
                content.filename = filename
                db.session.add(content)
                db.session.commit()

            # Associazione alla missione + MissionChange, come PUT /Marti/api/missions/<name>/contents
            already_assigned = db.session.execute(
                db.session.query(MissionContentMission).filter_by(
                    mission_content_id=content.id, mission_name=mission_name
                )
            ).first()
            if already_assigned:
                return jsonify(
                    {"success": True, "filename": filename, "mission": mission_name, "already_assigned": True}
                )

            mission_content_mission = MissionContentMission()
            mission_content_mission.mission_name = mission_name
            mission_content_mission.mission_content_id = content.id
            db.session.add(mission_content_mission)

            mission_change = MissionChange()
            mission_change.isFederatedChange = False
            mission_change.change_type = MissionChange.ADD_CONTENT
            mission_change.content_uid = content.uid
            mission_change.mission_name = mission_name
            mission_change.timestamp = datetime.now(timezone.utc)
            mission_change.creator_uid = username
            mission_change.server_time = datetime.now(timezone.utc)
            db.session.add(mission_change)
            db.session.commit()

            skyfi.notify_mission_change(mission_name, mission, mission_change, content)

            logger.info(f"MilSim/SkyFi: {filename} ({size} bytes) assegnato alla missione {mission_name} da {username}")
            return jsonify(
                {"success": True, "filename": filename, "mission": mission_name, "hash": file_hash, "size": size}
            )
        except BaseException as e:
            logger.error(f"Failed to assign order {uid} to mission: {e}")
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500
