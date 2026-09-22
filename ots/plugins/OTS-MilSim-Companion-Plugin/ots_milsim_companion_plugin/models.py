from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from opentakserver.extensions import db

import json

RSVP_STATUSES = ("not_configured", "present", "absent", "maybe")
MATCH_STATUSES = ("ready", "running", "ended")
MATCH_END_REASONS = ("time", "objective", "manual")


def _loads(value: str | None, fallback):
    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


class GameField(db.Model):
    """Anagrafica dei campi da gioco (sedi degli eventi)."""

    __tablename__ = "ec_game_fields"

    id = db.Column(Integer, primary_key=True)
    name = db.Column(String(255), nullable=False, unique=True)
    address = db.Column(String(512), nullable=True)
    latitude = db.Column(Float, nullable=True)
    longitude = db.Column(Float, nullable=True)
    description = db.Column(Text, nullable=True)
    active = db.Column(Boolean, nullable=False, default=True)

    events = relationship("CalendarEvent", back_populates="field")

    def serialize(self):
        return {
            "id": self.id,
            "name": self.name,
            "address": self.address,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "description": self.description,
            "active": self.active,
        }


class Player(db.Model):
    """Anagrafica giocatori della squadra, parallela agli account OTS.

    Un giocatore puo' essere associato (o no) a un account OpenTAKServer:
    presenze, punteggi e gradi fanno sempre riferimento al giocatore.
    """

    __tablename__ = "ec_players"

    id = db.Column(Integer, primary_key=True)
    first_name = db.Column(String(255), nullable=False, default="")
    last_name = db.Column(String(255), nullable=False, default="")
    callsign = db.Column(String(255), nullable=True)
    user_id = db.Column(Integer, ForeignKey("user.id"), nullable=True, unique=True)
    active = db.Column(Boolean, nullable=False, default=True)
    notes = db.Column(Text, nullable=True)
    created_at = db.Column(DateTime, nullable=False, default=datetime.utcnow)

    attendances = relationship("EventAttendance", back_populates="player", cascade="all, delete-orphan")
    score_row = relationship("PlayerScore", back_populates="player", uselist=False, cascade="all, delete-orphan")

    def display_name(self):
        full = f"{self.first_name} {self.last_name}".strip()
        if self.callsign and full:
            return f"{self.callsign} ({full})"
        return self.callsign or full or f"Giocatore {self.id}"

    def formal_name(self):
        """Nome Cognome (CALLSIGN) — usato nella lista presenze."""
        full = f"{self.first_name} {self.last_name}".strip()
        if full and self.callsign:
            return f"{full} ({self.callsign})"
        return full or self.callsign or f"Giocatore {self.id}"

    def serialize(self):
        return {
            "id": self.id,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "callsign": self.callsign,
            "user_id": self.user_id,
            "active": self.active,
            "notes": self.notes,
            "display_name": self.display_name(),
        }


class CalendarEvent(db.Model):
    """Evento di calendario: sede, inizio, fine, descrizione."""

    __tablename__ = "ec_events"

    id = db.Column(Integer, primary_key=True)
    title = db.Column(String(255), nullable=False)
    description = db.Column(Text, nullable=True)
    field_id = db.Column(Integer, ForeignKey("ec_game_fields.id"), nullable=False)
    start_time = db.Column(DateTime, nullable=False)
    end_time = db.Column(DateTime, nullable=False)
    source = db.Column(String(32), nullable=False, default="manual")  # manual | csv | ics
    external_uid = db.Column(String(512), nullable=True, unique=True)  # UID iCal per deduplicare gli import
    created_at = db.Column(DateTime, nullable=False, default=datetime.utcnow)

    field = relationship("GameField", back_populates="events")
    attendances = relationship("EventAttendance", back_populates="event", cascade="all, delete-orphan")
    guests = relationship("EventGuest", back_populates="event", cascade="all, delete-orphan")

    def serialize(self):
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "field_id": self.field_id,
            "field": self.field.serialize() if self.field else None,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "source": self.source,
        }


class EventAttendance(db.Model):
    """RSVP del giocatore + conferma presenza da parte dell'amministratore sul campo."""

    __tablename__ = "ec_attendances"
    __table_args__ = (UniqueConstraint("event_id", "player_id", name="uq_ec_attendance_event_player"),)

    id = db.Column(Integer, primary_key=True)
    event_id = db.Column(Integer, ForeignKey("ec_events.id"), nullable=False)
    player_id = db.Column(Integer, ForeignKey("ec_players.id"), nullable=False)
    rsvp_status = db.Column(String(32), nullable=False, default="not_configured")
    confirmed = db.Column(Boolean, nullable=False, default=False)
    confirmed_by = db.Column(Integer, ForeignKey("user.id"), nullable=True)
    confirmed_at = db.Column(DateTime, nullable=True)
    points_awarded = db.Column(Integer, nullable=False, default=0)

    event = relationship("CalendarEvent", back_populates="attendances")
    player = relationship("Player", back_populates="attendances")

    def serialize(self):
        return {
            "id": self.id,
            "event_id": self.event_id,
            "player_id": self.player_id,
            "rsvp_status": self.rsvp_status,
            "confirmed": self.confirmed,
            "confirmed_at": self.confirmed_at.isoformat() if self.confirmed_at else None,
            "points_awarded": self.points_awarded,
        }


class EventGuest(db.Model):
    """Ospite "in prova" registrato per un evento con nome e cognome (senza account OTS)."""

    __tablename__ = "ec_event_guests"

    id = db.Column(Integer, primary_key=True)
    event_id = db.Column(Integer, ForeignKey("ec_events.id"), nullable=False)
    first_name = db.Column(String(255), nullable=False)
    last_name = db.Column(String(255), nullable=False)
    added_by = db.Column(Integer, ForeignKey("user.id"), nullable=True)
    confirmed = db.Column(Boolean, nullable=False, default=False)
    confirmed_by = db.Column(Integer, ForeignKey("user.id"), nullable=True)
    confirmed_at = db.Column(DateTime, nullable=True)
    created_at = db.Column(DateTime, nullable=False, default=datetime.utcnow)

    event = relationship("CalendarEvent", back_populates="guests")

    def serialize(self):
        return {
            "id": self.id,
            "event_id": self.event_id,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "added_by": self.added_by,
            "confirmed": self.confirmed,
            "confirmed_at": self.confirmed_at.isoformat() if self.confirmed_at else None,
        }


class Rank(db.Model):
    """Grado/livello gerarchico: badge (immagine) e punteggio minimo per ottenerlo."""

    __tablename__ = "ec_ranks"

    id = db.Column(Integer, primary_key=True)
    name = db.Column(String(255), nullable=False, unique=True)
    min_score = db.Column(Integer, nullable=False, default=0)
    badge_filename = db.Column(String(512), nullable=True)

    def serialize(self):
        return {
            "id": self.id,
            "name": self.name,
            "min_score": self.min_score,
            "badge_filename": self.badge_filename,
        }


class PlayerScore(db.Model):
    """Punteggio accumulato da un giocatore ed eventuale grado assegnato manualmente."""

    __tablename__ = "ec_player_scores"

    id = db.Column(Integer, primary_key=True)
    player_id = db.Column(Integer, ForeignKey("ec_players.id"), nullable=False, unique=True)
    score = db.Column(Integer, nullable=False, default=0)
    manual_rank_id = db.Column(Integer, ForeignKey("ec_ranks.id"), nullable=True)

    player = relationship("Player", back_populates="score_row")
    manual_rank = relationship("Rank")

    def serialize(self):
        return {
            "id": self.id,
            "player_id": self.player_id,
            "score": self.score,
            "manual_rank_id": self.manual_rank_id,
        }


class GameTemplate(db.Model):
    """Template di missione: modalità, durata, marker, aree e data package.

    Marker e aree vivono come JSON sul template (niente join): l'editor della
    UI salva sempre il template intero.
    """

    __tablename__ = "gm_templates"

    id = db.Column(Integer, primary_key=True)
    title = db.Column(String(255), nullable=False)
    description = db.Column(Text, nullable=True)
    mode = db.Column(String(32), nullable=False)
    duration_minutes = db.Column(Integer, nullable=False, default=30)
    # Campo da gioco (anagrafica ec_game_fields), opzionale: centra la mappa
    # dell'editor sul campo e abilita l'anteprima del template
    field_id = db.Column(Integer, ForeignKey("ec_game_fields.id"), nullable=True)
    map_lat = db.Column(Float, nullable=True)
    map_lon = db.Column(Float, nullable=True)
    map_zoom = db.Column(Integer, nullable=True)
    markers_json = db.Column(Text, nullable=False, default="[]")   # [{type,label,lat,lon}]
    zones_json = db.Column(Text, nullable=False, default="[]")     # [{type,label,points:[[lat,lon],…]}]
    packages_json = db.Column(Text, nullable=False, default="[]")  # [hash data package OTS]
    # Al Play crea anche una missione Data Sync collegata alla partita, così
    # l'admin può definirne i dataset e assegnarla ai team con l'invito
    create_mission = db.Column(Boolean, nullable=False, default=False)

    field = relationship("GameField")
    created_at = db.Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    def serialize(self):
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "mode": self.mode,
            "duration_minutes": self.duration_minutes,
            "field_id": self.field_id,
            "field": self.field.serialize() if self.field else None,
            "map": {"lat": self.map_lat, "lon": self.map_lon, "zoom": self.map_zoom},
            "markers": _loads(self.markers_json, []),
            "zones": _loads(self.zones_json, []),
            "packages": _loads(self.packages_json, []),
            "create_mission": bool(self.create_mission),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class GameMatch(db.Model):
    """Partita creata dal Play di un template.

    Ciclo di vita: "ready" (Play: missione preparata, marker pushati),
    "running" (Inizia partita: luce verde + timer tenuto dal server),
    "ended" (tempo scaduto/obiettivo/manuale, chiusa dal match engine o dal GM).
    snapshot_json congela il template al momento del Play (il template può poi
    cambiare o sparire); cot_uids_json ricorda gli UID dei CoT pushati, per
    ripubblicarli o cancellarli dagli EUD al termine.
    """

    __tablename__ = "gm_matches"

    id = db.Column(Integer, primary_key=True)
    template_id = db.Column(Integer, ForeignKey("gm_templates.id"), nullable=True)
    title = db.Column(String(255), nullable=False)
    mode = db.Column(String(32), nullable=False)
    duration_minutes = db.Column(Integer, nullable=False)
    created_at = db.Column(DateTime, nullable=False, default=datetime.utcnow)
    started_at = db.Column(DateTime, nullable=True)   # null finché non si dà la luce verde
    ends_at = db.Column(DateTime, nullable=True)      # calcolata all'Inizia partita
    ended_at = db.Column(DateTime, nullable=True)
    status = db.Column(String(16), nullable=False, default="ready")
    started_by = db.Column(String(255), nullable=True)
    end_reason = db.Column(String(32), nullable=True)  # time | objective | manual
    winner = db.Column(String(255), nullable=True)
    # Team destinatari = team NATIVI di ATAK (tabella `teams` di OTS, popolata
    # dal colore squadra che ogni EUD trasmette nel <__group>): niente anagrafica
    # da mantenere, i giocatori si mettono nel colore giusto dal telefono.
    # Null/vuoto = broadcast a tutti gli EUD (comportamento storico). Con i team,
    # i CoT viaggiano mirati sull'exchange "dms" di OTS: gli spawn li vede solo
    # il proprio team (+ osservatori), il resto tutti i coinvolti.
    team_a_id = db.Column(Integer, nullable=True)   # groups.id di OTS
    team_b_id = db.Column(Integer, nullable=True)   # groups.id di OTS
    observers_json = db.Column(Text, nullable=False, default="[]")  # [groups.id osservatori]
    # Missione Data Sync creata al Play (se il template ha create_mission)
    mission_name = db.Column(String(255), nullable=True)
    snapshot_json = db.Column(Text, nullable=False, default="{}")
    cot_uids_json = db.Column(Text, nullable=False, default="[]")  # [{uid, cot_type, audience}]

    def serialize(self):
        now = datetime.utcnow()
        remaining = int((self.ends_at - now).total_seconds()) if self.ends_at else None
        return {
            "id": self.id,
            "template_id": self.template_id,
            "title": self.title,
            "mode": self.mode,
            "duration_minutes": self.duration_minutes,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "started_at": self.started_at.isoformat() + "Z" if self.started_at else None,
            "ends_at": self.ends_at.isoformat() + "Z" if self.ends_at else None,
            "ended_at": self.ended_at.isoformat() + "Z" if self.ended_at else None,
            "status": self.status,
            "started_by": self.started_by,
            "end_reason": self.end_reason,
            "winner": self.winner,
            "team_a_id": self.team_a_id,
            "team_b_id": self.team_b_id,
            "observer_team_ids": _loads(self.observers_json, []),
            "mission_name": self.mission_name,
            "remaining_seconds": max(0, remaining) if (self.status == "running" and remaining is not None) else 0,
            "expired": self.status == "running" and remaining is not None and remaining <= 0,
            "snapshot": _loads(self.snapshot_json, {}),
        }


class EngineLease(db.Model):
    """Lease del match engine: una sola istanza del plugin alla volta fa i tick.

    OTS può caricare i plugin in più processi (main, cot_parser, eud_handler):
    il thread del match engine gira ovunque, ma solo chi detiene il lease
    (riga id=1, heartbeat rinnovato a ogni tick) esegue davvero la logica.
    """

    __tablename__ = "gm_engine_lease"

    id = db.Column(Integer, primary_key=True)  # sempre 1
    holder = db.Column(String(64), nullable=True)
    heartbeat = db.Column(DateTime, nullable=True)


class SkyfiHiddenOrder(db.Model):
    """Ordini SkyFi rimossi logicamente dalla tab SkyFi.

    Su SkyFi gli ordini non si possono cancellare (es. pending che non
    verranno mai evasi): qui si tiene l'elenco degli uid da non mostrare
    più in GET /orders. Ripristinabili in ogni momento.
    """

    __tablename__ = "ec_skyfi_hidden_orders"

    id = db.Column(Integer, primary_key=True)
    order_uid = db.Column(String(255), nullable=False, unique=True)
    order_code = db.Column(String(255), nullable=True)
    hidden_by = db.Column(String(255), nullable=True)
    hidden_at = db.Column(DateTime, nullable=False, default=datetime.utcnow)

    def serialize(self):
        return {
            "order_uid": self.order_uid,
            "order_code": self.order_code,
            "hidden_by": self.hidden_by,
            "hidden_at": self.hidden_at.isoformat() + "Z" if self.hidden_at else None,
        }


class MeshChannelMap(db.Model):
    """Mappatura canale Meshtastic → gruppo TAK di OpenTAKServer.

    La chiave del canale può essere il nome (`ALPHA`), l'indice (`0`) o
    entrambi: dal feed MQTT arrivano tutti e due, dal relay ATAK non arriva
    nulla (vedi docs/meshtastic-architettura.md). Il gruppo è un gruppo vero
    della tabella `groups` di OTS: si salva l'id e il nome al momento della
    scelta, ma l'instradamento risolve sempre il nome corrente dall'id.
    """

    __tablename__ = "msh_channel_map"

    id = db.Column(Integer, primary_key=True)
    channel_name = db.Column(String(255), nullable=True)
    channel_index = db.Column(Integer, nullable=True)
    group_id = db.Column(Integer, nullable=False)
    group_name = db.Column(String(255), nullable=True)
    enabled = db.Column(Boolean, nullable=False, default=True)
    created_at = db.Column(DateTime, nullable=False, default=datetime.utcnow)

    def serialize(self):
        return {
            "id": self.id,
            "channel_name": self.channel_name,
            "channel_index": self.channel_index,
            "group_id": self.group_id,
            "group_name": self.group_name,
            "enabled": self.enabled,
        }


class MeshTag(db.Model):
    """Identità persistente di un tag Meshtastic + override dell'amministratore.

    La telemetria viva (posizione, RSSI, pacchetti recenti) sta in memoria nel
    registry di `meshtastic.py`: qui si salva solo quel che deve sopravvivere
    a un riavvio, cioè l'anagrafica del tag e le scelte manuali fatte in UI.
    """

    __tablename__ = "msh_tags"

    id = db.Column(Integer, primary_key=True)
    # Identità stabile: node id Meshtastic se noto, altrimenti uid del CoT
    tag_key = db.Column(String(255), nullable=False, unique=True)
    node_id = db.Column(String(64), nullable=True)
    cot_uid = db.Column(String(255), nullable=True)
    callsign = db.Column(String(255), nullable=True)
    long_name = db.Column(String(255), nullable=True)
    short_name = db.Column(String(64), nullable=True)
    first_seen = db.Column(DateTime, nullable=True)
    last_seen = db.Column(DateTime, nullable=True)
    # Override manuale: il canale che l'amministratore dichiara per questo tag
    # quando la trasmissione non lo porta (caso relay ATAK). Mai dedotto.
    manual_channel_name = db.Column(String(255), nullable=True)
    manual_channel_index = db.Column(Integer, nullable=True)
    # Override ancora più diretto: gruppo forzato, salta la mappatura canali
    manual_group_id = db.Column(Integer, nullable=True)
    notes = db.Column(Text, nullable=True)

    def serialize(self):
        return {
            "tag_key": self.tag_key,
            "node_id": self.node_id,
            "cot_uid": self.cot_uid,
            "callsign": self.callsign,
            "long_name": self.long_name,
            "short_name": self.short_name,
            "first_seen": self.first_seen.isoformat() + "Z" if self.first_seen else None,
            "last_seen": self.last_seen.isoformat() + "Z" if self.last_seen else None,
            "manual_channel_name": self.manual_channel_name,
            "manual_channel_index": self.manual_channel_index,
            "manual_group_id": self.manual_group_id,
            "notes": self.notes,
        }


PLUGIN_TABLES = [
    GameField.__table__,
    Player.__table__,
    CalendarEvent.__table__,
    EventAttendance.__table__,
    EventGuest.__table__,
    Rank.__table__,
    PlayerScore.__table__,
    GameTemplate.__table__,
    GameMatch.__table__,
    EngineLease.__table__,
    SkyfiHiddenOrder.__table__,
    MeshChannelMap.__table__,
    MeshTag.__table__,
]
