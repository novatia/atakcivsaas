# Match engine: il thread che tiene il tempo delle partite lato server.
#
# Un thread daemon fa un tick al secondo; a ogni tick chiude le partite
# "running" arrivate a ends_at (annuncio in chat + cancellazione marker) e
# decreta l'esito tramite l'arbitro (referee) della modalità. Gli arbitri sono
# tipizzati: ogni modalità dichiara i propri eventi di partita (es. Bomb:
# piazzata/disinnescata/esplosa, che può finire prima del tempo) e come si
# decide il vincitore allo scadere.
#
# OTS può caricare i plugin in più processi (main, cot_parser, eud_handler):
# il thread parte ovunque, ma solo chi detiene il lease su gm_engine_lease
# (heartbeat rinnovato a ogni tick, takeover dopo 10 s di silenzio) esegue
# davvero la logica, così gli annunci non escono doppi.
import json
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone

from flask import current_app as app
from sqlalchemy import or_, update

from opentakserver.extensions import db, logger

from . import cot
from .game_modes import GAME_MODES
from .models import EngineLease, GameMatch

TICK_SECONDS = 1
LEASE_TIMEOUT = timedelta(seconds=10)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _sender_uid() -> str:
    return f"GameMaster.{app.config.get('OTS_NODE_ID', 'ots')}"


def _callsign() -> str:
    return app.config.get("OTS_EVENTCALENDAR_GM_CALLSIGN") or "Game Master"


# ----------------------------------------------------------------------
# Arbitri: uno per modalità (condizione di vittoria ed eventi di partita)
# ----------------------------------------------------------------------


class TimeReferee:
    """Base: la partita finisce solo allo scadere del tempo, esito sul campo."""

    def time_up(self, match: GameMatch) -> tuple[str | None, str]:
        """(winner, esito) quando scade il tempo."""
        return None, "Esito da decretare sul campo."


class CtfReferee(TimeReferee):
    def time_up(self, match: GameMatch):
        return None, "Vince chi ha catturato più bandiere (conteggio sul campo)."


class TdmReferee(TimeReferee):
    def time_up(self, match: GameMatch):
        return None, "Vince chi ha più eliminazioni (conteggio sul campo)."


class BombReferee(TimeReferee):
    """La partita può finire prima del tempo: disinnesco o esplosione
    (eventi dichiarati nell'anagrafica della modalità, vedi match_events_for)."""

    def time_up(self, match: GameMatch):
        return "Difensori", "Tempo scaduto senza esplosione: vincono i difensori."


class DomReferee(TimeReferee):
    """Vittoria a 100 punti o allo scadere: il punteggio server non è ancora
    tracciato (arriverà con l'orchestratore in campo), per ora esito manuale."""

    def time_up(self, match: GameMatch):
        target = GAME_MODES.get(match.mode, {}).get("target_score", 100)
        return None, f"Vince il team con più punti dominio (target {target}; conteggio sul campo)."


REFEREES = {
    "ctf": CtfReferee(),
    "bomb": BombReferee(),
    "tdm": TdmReferee(),
    "dom": DomReferee(),
}


def referee_for(mode: str) -> TimeReferee:
    return REFEREES.get(mode) or TimeReferee()


def match_events_for(mode: str) -> dict:
    """Eventi di partita della modalità (dall'anagrafica): {chiave: {label,
    ends, winner, chat}}. Se ends=True l'evento chiude la partita prima del tempo."""
    return GAME_MODES.get(mode, {}).get("events") or {}


# ----------------------------------------------------------------------
# Destinatari: gruppi della partita -> uid degli EUD
# ----------------------------------------------------------------------


def resolve_targets(match: GameMatch) -> dict | None:
    """Risolve i gruppi della partita in insiemi di uid EUD, al momento
    dell'invio (i cambi di appartenenza valgono subito, es. con «Ripubblica»).

    I gruppi sono i gruppi/canali TAK di OTS (tabella `groups`, membri in
    `groups_users` per utente): gli EUD di un gruppo sono i dispositivi
    (EUD.user_id) degli utenti con una membership abilitata, in qualunque
    direzione IN/OUT. None = partita senza gruppi: broadcast storico a tutti.
    Altrimenti {"team_a": Team A + osservatori, "team_b": Team B + osservatori,
    "all": tutti i coinvolti} — è la mappa delle audience dei marker.
    """
    observer_ids = json.loads(match.observers_json or "[]")
    if not (match.team_a_id or match.team_b_id or observer_ids):
        return None

    from opentakserver.models.EUD import EUD
    from opentakserver.models.GroupUser import GroupUser

    def members(group_id) -> set:
        if not group_id:
            return set()
        user_ids = {
            m.user_id
            for m in db.session.query(GroupUser).filter_by(group_id=int(group_id), enabled=True).all()
        }
        if not user_ids:
            return set()
        rows = db.session.query(EUD).filter(EUD.user_id.in_(user_ids)).all()
        return {row.uid for row in rows}

    team_a = members(match.team_a_id)
    team_b = members(match.team_b_id)
    observers: set = set()
    for group_id in observer_ids:
        observers |= members(group_id)

    return {
        "team_a": team_a | observers,
        "team_b": team_b | observers,
        "all": team_a | team_b | observers,
    }


def audience_targets(targets: dict | None, audience: str):
    """Destinatari per una audience ("team_a"/"team_b"/"all"); None = broadcast."""
    if targets is None:
        return None
    return targets.get(audience) or targets["all"]


# ----------------------------------------------------------------------
# Chiusura partita (usata dal tick, dagli eventi e dal Termina manuale)
# ----------------------------------------------------------------------


def finish_match(match: GameMatch, end_reason: str, winner: str | None, chat_text: str) -> bool:
    """Chiude la partita: cancella i marker dagli EUD che li avevano ricevuti
    (stessa audience dell'invio), annuncia in chat ai coinvolti e salva
    esito/timestamp. Ritorna False se la pubblicazione è fallita (la partita
    viene chiusa comunque: i marker spariranno con lo stale)."""
    targets = resolve_targets(match)
    uids = json.loads(match.cot_uids_json or "[]")
    items = []
    for u in uids:
        delete = cot.delete_event(u["uid"], u["cot_type"] or "a-u-G")
        items.append((delete, audience_targets(targets, u.get("audience", "all"))))
        if targets is not None:
            # Copia per il cot_parser: ripulisce anche la tabella markers di
            # OTS (la web map del server), simmetrica alla copia del Play
            items.append((delete, None))
    items.append(
        (cot.geochat_event(chat_text, _sender_uid(), _callsign()), audience_targets(targets, "all"))
    )
    broadcast_ok = cot.deliver(items)

    match.status = "ended"
    match.ended_at = _utcnow()
    match.end_reason = end_reason
    match.winner = winner
    db.session.commit()
    return broadcast_ok


# ----------------------------------------------------------------------
# Lease + loop
# ----------------------------------------------------------------------


def _acquire_lease(holder: str) -> bool:
    """Prende o rinnova il lease in modo atomico (UPDATE condizionato)."""
    now = _utcnow()
    cutoff = now - LEASE_TIMEOUT
    try:
        lease = db.session.get(EngineLease, 1)
        if not lease:
            db.session.add(EngineLease(id=1, holder=holder, heartbeat=now))
            db.session.commit()
            return True
        result = db.session.execute(
            update(EngineLease)
            .where(
                EngineLease.id == 1,
                or_(EngineLease.holder == holder, EngineLease.heartbeat.is_(None), EngineLease.heartbeat < cutoff),
            )
            .values(holder=holder, heartbeat=now)
        )
        db.session.commit()
        return bool(result.rowcount)
    except BaseException:
        db.session.rollback()
        return False


def _tick() -> None:
    now = _utcnow()
    expired = (
        db.session.query(GameMatch)
        .filter(GameMatch.status == "running", GameMatch.ends_at.isnot(None), GameMatch.ends_at <= now)
        .all()
    )
    for match in expired:
        referee = referee_for(match.mode)
        winner, esito = referee.time_up(match)
        mode_name = GAME_MODES.get(match.mode, {}).get("name", match.mode)
        chat = f"🏁 TEMPO SCADUTO — partita terminata: {match.title} ({mode_name}). {esito}"
        finish_match(match, "time", winner, chat)
        logger.info(f"MilSim engine: partita '{match.title}' chiusa a tempo scaduto (winner={winner})")


def _loop(flask_app) -> None:
    holder = uuid.uuid4().hex
    logger.info(f"MilSim engine: thread avviato (holder {holder[:8]}, tick {TICK_SECONDS}s)")
    while True:
        time.sleep(TICK_SECONDS)
        try:
            with flask_app.app_context():
                if not _acquire_lease(holder):
                    continue
                _tick()
        except BaseException as e:
            logger.error(f"MilSim engine: errore nel tick: {e}")
            logger.debug(traceback.format_exc())


_started = False


def start_engine(flask_app) -> None:
    """Avviato da activate(): un solo thread per processo (il lease fa il resto)."""
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, args=(flask_app,), name="milsim-match-engine", daemon=True).start()
