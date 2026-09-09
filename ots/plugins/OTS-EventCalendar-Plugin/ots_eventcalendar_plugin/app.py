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
from opentakserver.models.user import User
from opentakserver.plugins.Plugin import Plugin

from .default_config import DefaultConfig
from .models import (
    PLUGIN_TABLES,
    RSVP_STATUSES,
    CalendarEvent,
    EventAttendance,
    EventGuest,
    GameField,
    Rank,
    UserScore,
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


def _badges_folder() -> str:
    folder = os.path.join(
        app.config.get("OTS_DATA_FOLDER"), "plugins", "ots_eventcalendar_plugin", "badges"
    )
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


def _get_or_create_score(user_id: int) -> UserScore:
    score = db.session.query(UserScore).filter_by(user_id=user_id).first()
    if not score:
        score = UserScore(user_id=user_id, score=0)
        db.session.add(score)
        db.session.flush()
    return score


def _rank_for_score(score_value: int, ranks: list) -> dict | None:
    best = None
    for rank in ranks:
        if rank.min_score <= score_value and (best is None or rank.min_score > best.min_score):
            best = rank
    return best.serialize() if best else None


def _resolve_rank(score_row: UserScore | None, ranks: list) -> dict | None:
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
        logger.error(f"EventCalendar maintenance: failed to broadcast marker deletions: {e}")
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


class EventCalendarPlugin(Plugin):
    metadata = pathlib.Path(__file__).resolve().parent.name
    url_prefix = f"/api/plugins/{metadata.lower()}"
    blueprint = Blueprint("EventCalendarPlugin", __name__, url_prefix=url_prefix)

    def activate(self, app: Flask, enabled: bool = True):
        self._app = app
        self._load_config()
        self.load_metadata()

        try:
            with app.app_context():
                # Crea le tabelle del plugin se non esistono (non tocca le tabelle di OTS)
                db.metadata.create_all(bind=db.engine, tables=PLUGIN_TABLES, checkfirst=True)

                # Seed dei gradi di default alla prima attivazione
                if not db.session.query(Rank).first():
                    for name, min_score in DEFAULT_RANKS:
                        db.session.add(Rank(name=name, min_score=min_score))
                    db.session.commit()
                    logger.info("EventCalendar: seeded default ranks")

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
            score_row = db.session.query(UserScore).filter_by(user_id=current_user.id).first()
            return jsonify(
                {
                    "user_id": current_user.id,
                    "username": current_user.username,
                    "roles": [role.name for role in current_user.roles],
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
                    if attendance.user_id == current_user.id:
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

            data = event.serialize()
            counts = {"present": 0, "absent": 0, "maybe": 0, "confirmed": 0}
            my_rsvp = "not_configured"
            for attendance in event.attendances:
                if attendance.rsvp_status in counts:
                    counts[attendance.rsvp_status] += 1
                if attendance.confirmed:
                    counts["confirmed"] += 1
                if attendance.user_id == current_user.id:
                    my_rsvp = attendance.rsvp_status
            data["counts"] = counts
            data["my_rsvp"] = my_rsvp
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
                    score = _get_or_create_score(attendance.user_id)
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

            attendance = (
                db.session.query(EventAttendance)
                .filter_by(event_id=event_id, user_id=current_user.id)
                .first()
            )
            if not attendance:
                attendance = EventAttendance(event_id=event_id, user_id=current_user.id)
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

            attendance_by_user = {a.user_id: a for a in event.attendances}
            users = db.session.query(User).filter_by(active=True).order_by(User.username).all()

            results = []
            for user in users:
                attendance = attendance_by_user.get(user.id)
                results.append(
                    {
                        "user_id": user.id,
                        "username": user.username,
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
            user_id = data.get("user_id")
            confirmed = bool(data.get("confirmed"))
            if not user_id:
                return jsonify({"success": False, "error": "user_id è obbligatorio"}), 400

            event = db.session.get(CalendarEvent, event_id)
            if not event:
                return jsonify({"success": False, "error": "Evento non trovato"}), 404
            if not db.session.get(User, int(user_id)):
                return jsonify({"success": False, "error": "Utente non trovato"}), 404

            attendance = (
                db.session.query(EventAttendance)
                .filter_by(event_id=event_id, user_id=int(user_id))
                .first()
            )
            if not attendance:
                attendance = EventAttendance(event_id=event_id, user_id=int(user_id))
                db.session.add(attendance)
                db.session.flush()

            points = int(app.config.get("OTS_EVENTCALENDAR_POINTS_PER_PRESENCE", 10))
            score = _get_or_create_score(int(user_id))

            if confirmed and not attendance.confirmed:
                attendance.confirmed = True
                attendance.confirmed_by = current_user.id
                attendance.confirmed_at = datetime.utcnow()
                attendance.points_awarded = points
                score.score += points
            elif not confirmed and attendance.confirmed:
                attendance.confirmed = False
                attendance.confirmed_by = None
                attendance.confirmed_at = None
                score.score = max(0, score.score - attendance.points_awarded)
                attendance.points_awarded = 0

            db.session.commit()
            return jsonify(
                {"success": True, "attendance": attendance.serialize(), "score": score.score}
            )
        except BaseException as e:
            db.session.rollback()
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 400

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

            db.session.query(UserScore).filter_by(manual_rank_id=rank_id).update({"manual_rank_id": None})
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
            logger.info(f"EventCalendar maintenance: {current_user.username} deleted {deleted} rows from {target}")

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
    # Classifica e gestione punteggi/gradi utente
    # ------------------------------------------------------------------

    @staticmethod
    @auth_required()
    @blueprint.route("/leaderboard")
    def leaderboard():
        try:
            ranks = db.session.query(Rank).order_by(Rank.min_score).all()
            users = db.session.query(User).filter_by(active=True).order_by(User.username).all()
            scores = {s.user_id: s for s in db.session.query(UserScore).all()}

            results = []
            for user in users:
                score_row = scores.get(user.id)
                results.append(
                    {
                        "user_id": user.id,
                        "username": user.username,
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
    @blueprint.route("/users/<int:user_id>/rank", methods=["POST"])
    def set_user_rank(user_id):
        """Assegna manualmente un grado a un utente (rank_id null = torna al calcolo per punteggio)."""
        try:
            if not db.session.get(User, user_id):
                return jsonify({"success": False, "error": "Utente non trovato"}), 404

            rank_id = (request.json or {}).get("rank_id")
            if rank_id is not None and not db.session.get(Rank, int(rank_id)):
                return jsonify({"success": False, "error": "Grado non trovato"}), 404

            score = _get_or_create_score(user_id)
            score.manual_rank_id = int(rank_id) if rank_id is not None else None
            db.session.commit()
            return jsonify({"success": True, "user_score": score.serialize()})
        except BaseException as e:
            db.session.rollback()
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 400

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/users/<int:user_id>/score", methods=["POST"])
    def set_user_score(user_id):
        """Corregge manualmente il punteggio di un utente."""
        try:
            if not db.session.get(User, user_id):
                return jsonify({"success": False, "error": "Utente non trovato"}), 404

            value = (request.json or {}).get("score")
            if not isinstance(value, int) or value < 0:
                return jsonify({"success": False, "error": "score deve essere un intero >= 0"}), 400

            score = _get_or_create_score(user_id)
            score.score = value
            db.session.commit()
            return jsonify({"success": True, "user_score": score.serialize()})
        except BaseException as e:
            db.session.rollback()
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 400
