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

RSVP_STATUSES = ("not_configured", "present", "absent", "maybe")


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
    """RSVP dell'utente + conferma presenza da parte dell'amministratore sul campo."""

    __tablename__ = "ec_attendances"
    __table_args__ = (UniqueConstraint("event_id", "user_id", name="uq_ec_attendance_event_user"),)

    id = db.Column(Integer, primary_key=True)
    event_id = db.Column(Integer, ForeignKey("ec_events.id"), nullable=False)
    user_id = db.Column(Integer, ForeignKey("user.id"), nullable=False)
    rsvp_status = db.Column(String(32), nullable=False, default="not_configured")
    confirmed = db.Column(Boolean, nullable=False, default=False)
    confirmed_by = db.Column(Integer, ForeignKey("user.id"), nullable=True)
    confirmed_at = db.Column(DateTime, nullable=True)
    points_awarded = db.Column(Integer, nullable=False, default=0)

    event = relationship("CalendarEvent", back_populates="attendances")

    def serialize(self):
        return {
            "id": self.id,
            "event_id": self.event_id,
            "user_id": self.user_id,
            "rsvp_status": self.rsvp_status,
            "confirmed": self.confirmed,
            "confirmed_at": self.confirmed_at.isoformat() if self.confirmed_at else None,
            "points_awarded": self.points_awarded,
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


class UserScore(db.Model):
    """Punteggio accumulato da un utente ed eventuale grado assegnato manualmente."""

    __tablename__ = "ec_user_scores"

    id = db.Column(Integer, primary_key=True)
    user_id = db.Column(Integer, ForeignKey("user.id"), nullable=False, unique=True)
    score = db.Column(Integer, nullable=False, default=0)
    manual_rank_id = db.Column(Integer, ForeignKey("ec_ranks.id"), nullable=True)

    manual_rank = relationship("Rank")

    def serialize(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "score": self.score,
            "manual_rank_id": self.manual_rank_id,
        }


PLUGIN_TABLES = [
    GameField.__table__,
    CalendarEvent.__table__,
    EventAttendance.__table__,
    Rank.__table__,
    UserScore.__table__,
]
