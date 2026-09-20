import csv
import io
import json
import os
import pathlib
import traceback
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import yaml
from flask import (
    Blueprint,
    Flask,
    current_app as app,
    jsonify,
    request,
    send_from_directory,
)
from flask_security import auth_required, current_user, roles_accepted

from opentakserver.extensions import db, logger
from opentakserver.models.DataPackage import DataPackage
from opentakserver.models.user import User
from opentakserver.plugins.Plugin import Plugin

from . import cot
from .default_config import DefaultConfig
from .game_modes import GAME_MODES, MARKER_TYPES, serialize_registry, validate_template
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
    }, None


def _match_events(match: GameMatch, uids: list[dict]) -> list:
    """Ricostruisce i CoT di marker e aree della partita (Play e Ripubblica)."""
    snapshot = json.loads(match.snapshot_json)
    stale = match.ends_at.replace(tzinfo=timezone.utc) + STALE_GRACE
    remarks = f"{snapshot.get('title', match.title)} — {GAME_MODES[match.mode]['name']}, fine {match.ends_at.strftime('%H:%M')} UTC"

    events = []
    items = [("marker", m) for m in snapshot.get("markers", [])] + [("zone", z) for z in snapshot.get("zones", [])]
    for entry, item in zip(uids, items):
        kind, data = item
        if kind == "marker":
            events.append(cot.marker_event(entry["uid"], data, stale, remarks))
        else:
            events.append(cot.zone_event(entry["uid"], data, stale, remarks))
    return events


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

            from opentakserver.models.EUD import EUD
            from opentakserver.models.Point import Point

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

            return jsonify(
                {
                    "event": event.serialize(),
                    "start": start_utc.timestamp(),
                    "end": end_utc.timestamp(),
                    "step": step,
                    "tracks": [
                        {"uid": uid, "callsign": callsigns.get(uid) or uid, "points": pts}
                        for uid, pts in sorted(
                            tracks.items(), key=lambda kv: (callsigns.get(kv[0]) or kv[0]).lower()
                        )
                    ],
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

            now = _utcnow()
            match = GameMatch(
                template_id=template.id,
                title=template.title,
                mode=template.mode,
                duration_minutes=template.duration_minutes,
                started_at=now,
                ends_at=now + timedelta(minutes=template.duration_minutes),
                status="running",
                started_by=current_user.username,
                snapshot_json=json.dumps(snapshot),
            )

            # UID stabili per marker e aree: servono per ripubblicare e cancellare
            run = uuid.uuid4().hex[:10]
            uids = [
                {"uid": f"GM.{run}.m{i}", "cot_type": None}
                for i in range(len(snapshot["markers"]) + len(snapshot["zones"]))
            ]
            for i, marker in enumerate(snapshot["markers"]):
                uids[i]["cot_type"] = MARKER_TYPES[marker["type"]]["cot_type"]
            for j in range(len(snapshot["zones"])):
                uids[len(snapshot["markers"]) + j]["cot_type"] = "u-d-f"
            match.cot_uids_json = json.dumps(uids)
            events = _match_events(match, uids)

            package_events, missing = _package_events(snapshot.get("packages", []))
            events.extend(package_events)

            mode_name = GAME_MODES[template.mode]["name"]
            chat = f"🎮 Partita iniziata: {template.title} ({mode_name}), durata {template.duration_minutes} minuti."
            if template.description:
                chat += f" {template.description}"
            events.append(cot.geochat_event(chat, _gm_sender_uid(), _gm_callsign()))

            if not cot.broadcast(events):
                return jsonify(
                    {"success": False, "error": "Push agli EUD fallito (RabbitMQ non raggiungibile): partita non creata"}
                ), 502

            db.session.add(match)
            db.session.commit()

            logger.info(
                f"MilSim: partita '{match.title}' ({mode_name}) avviata da {current_user.username}: "
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
            matches = db.session.query(GameMatch).order_by(GameMatch.started_at.desc()).limit(100).all()
            return jsonify([m.serialize() for m in matches])
        except BaseException as e:
            logger.error(f"MilSim: failed to get matches: {e}")
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
            if match.status != "running":
                return jsonify({"success": False, "error": "La partita è già terminata"}), 400
            if match.ends_at <= _utcnow():
                return jsonify({"success": False, "error": "La partita è scaduta: i marker non vengono ripubblicati"}), 400

            uids = json.loads(match.cot_uids_json)
            if not cot.broadcast(_match_events(match, uids)):
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
        try:
            match = db.session.get(GameMatch, match_id)
            if not match:
                return jsonify({"success": False, "error": "Partita non trovata"}), 404
            if match.status != "running":
                return jsonify({"success": False, "error": "La partita è già terminata"}), 400

            uids = json.loads(match.cot_uids_json)
            events = [cot.delete_event(u["uid"], u["cot_type"] or "a-u-G") for u in uids]
            events.append(
                cot.geochat_event(f"🏁 Partita terminata: {match.title}.", _gm_sender_uid(), _gm_callsign())
            )
            broadcast_ok = cot.broadcast(events)

            match.status = "ended"
            match.ended_at = _utcnow()
            db.session.commit()

            logger.info(f"MilSim: partita '{match.title}' terminata da {current_user.username}")
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
