# Storico delle squadre per il replay.
#
# OpenTAKServer conosce solo il presente: chi è in quale gruppo
# (groups_users), a chi è assegnato un tag (msh_tags.owner_user_id), quale
# gruppo è Team A/B/osservatori (config). Un replay colorato con lo stato di
# oggi sbaglia appena qualcuno cambia squadra. Qui ogni fatto diventa un
# intervallo [valid_from, valid_to) nella tabella ms_membership_history.
#
# sync() confronta lo stato attuale con gli intervalli aperti: chiude quelli
# spariti e apre quelli nuovi. Gira ogni SYNC_SECONDS nel loop del motore
# partite (così cattura anche le modifiche fatte dalla web UI di OTS, che
# non ha hook) e subito dopo ogni modifica fatta dal plugin (ora esatta).
#
# Il diff e la ricostruzione sono funzioni pure, verificate dai test; il DB
# sta solo in sync() e load_timeline().

import time
from datetime import datetime, timezone

from opentakserver.extensions import db, logger

GROUP = "group"
TAG = "tag"
ROLE = "role"

SYNC_SECONDS = 30
_next_sync = 0.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def current_facts(memberships, tag_owners, roles: dict) -> set:
    """Fatti validi adesso, come tuple (kind, subject, ref_id, direction).

    memberships: (user_id, group_id, direction) abilitate; tag_owners:
    (tag_key, user_id) dei tag assegnati; roles: teams.roles_from_config.
    """
    facts = {(GROUP, str(user_id), int(group_id), direction) for user_id, group_id, direction in memberships}
    facts |= {(TAG, tag_key, int(user_id), None) for tag_key, user_id in tag_owners if user_id}
    for role in ("team_a", "team_b"):
        if roles.get(role):
            facts.add((ROLE, role, int(roles[role]), None))
    facts |= {(ROLE, "observers", int(g), None) for g in roles.get("observers") or [] if g}
    return facts


def diff(open_rows: dict, facts: set) -> tuple[list, list]:
    """(id da chiudere, fatti da aprire). open_rows: fatto → id della riga aperta."""
    to_close = [row_id for fact, row_id in open_rows.items() if fact not in facts]
    to_open = sorted(facts - open_rows.keys(), key=lambda f: (f[0], f[1], f[2], f[3] or ""))
    return to_close, to_open


def roles_on_disk(config) -> dict:
    """Mappatura Team da config.yml, non da app.config: il salvataggio
    aggiorna app.config solo nel processo che riceve la richiesta, e il loop
    del motore può girare in un altro con la copia vecchia (lo storico
    oscillerebbe fra le due)."""
    import os

    import yaml

    from . import teams

    try:
        with open(os.path.join(config.get("OTS_DATA_FOLDER"), "config.yml")) as config_file:
            on_disk = yaml.safe_load(config_file) or {}
        return teams.roles_from_config({**config, **on_disk})
    except BaseException:
        return teams.roles_from_config(config)


def sync(config, force: bool = False) -> None:
    """Allinea lo storico allo stato attuale. Mai fatale: al peggio il
    cambio viene registrato al giro successivo."""
    global _next_sync
    now_mono = time.monotonic()
    if not force and now_mono < _next_sync:
        return
    _next_sync = now_mono + SYNC_SECONDS

    from opentakserver.models.GroupUser import GroupUser

    from .models import MembershipHistory, MeshTag

    try:
        memberships = [
            (m.user_id, m.group_id, m.direction)
            for m in db.session.query(GroupUser).filter_by(enabled=True).all()
        ]
        tag_owners = [(t.tag_key, t.owner_user_id) for t in db.session.query(MeshTag).all()]
        facts = current_facts(memberships, tag_owners, roles_on_disk(config))

        open_rows = {}
        for row in db.session.query(MembershipHistory).filter(MembershipHistory.valid_to.is_(None)).all():
            fact = (row.kind, row.subject, row.ref_id, row.direction)
            if fact in open_rows:
                row.valid_to = row.valid_from  # doppione (due sync concorrenti): si annulla
            else:
                open_rows[fact] = row.id

        to_close, to_open = diff(open_rows, facts)
        if not to_close and not to_open:
            db.session.commit()
            return
        now = _utcnow()
        if to_close:
            db.session.query(MembershipHistory).filter(MembershipHistory.id.in_(to_close)).update(
                {MembershipHistory.valid_to: now}, synchronize_session=False
            )
        for kind, subject, ref_id, direction in to_open:
            db.session.add(MembershipHistory(
                kind=kind, subject=subject, ref_id=ref_id, direction=direction, valid_from=now,
            ))
        db.session.commit()
        logger.debug(f"MilSim history: {len(to_close)} chiusi, {len(to_open)} aperti")
    except BaseException as e:
        db.session.rollback()
        logger.warning(f"MilSim history: allineamento storico squadre fallito: {e}")


class Timeline:
    """Chi era dove all'istante t (secondi epoch UTC).

    rows: (kind, subject, ref_id, direction, from_ts, to_ts|None).
    Prima dell'inizio dello storico (`since`, il primo valid_from registrato)
    non si sa nulla: lì si usa lo stato attuale e lo si dichiara.
    """

    def __init__(self, rows, since: float | None):
        self.since = since
        self._rows: dict = {}
        for kind, subject, ref_id, direction, start, end in rows:
            self._rows.setdefault((kind, str(subject)), []).append((start, end, ref_id, direction))

    def covers(self, t: float) -> bool:
        return self.since is not None and t >= self.since

    def _at(self, kind: str, subject, t: float) -> list:
        return [
            (ref_id, direction)
            for start, end, ref_id, direction in self._rows.get((kind, str(subject)), [])
            if start <= t and (end is None or t < end)
        ]

    def groups_at(self, user_id, t: float) -> list:
        """(group_id, direction) dell'utente all'istante t."""
        return self._at(GROUP, user_id, t)

    def tag_owner_at(self, tag_key: str, t: float):
        owners = self._at(TAG, tag_key, t)
        return owners[0][0] if owners else None

    def roles_at(self, t: float) -> dict:
        return {
            "team_a": next((g for g, _ in self._at(ROLE, "team_a", t)), None),
            "team_b": next((g for g, _ in self._at(ROLE, "team_b", t)), None),
            "observers": [g for g, _ in self._at(ROLE, "observers", t)],
        }


def load_timeline(start: datetime, end: datetime) -> Timeline:
    """Intervalli che toccano la finestra [start, end] (UTC naive)."""
    from sqlalchemy import func, or_

    from .models import MembershipHistory

    def ts(value):
        return value.replace(tzinfo=timezone.utc).timestamp() if value else None

    since = db.session.query(func.min(MembershipHistory.valid_from)).scalar()
    rows = (
        db.session.query(MembershipHistory)
        .filter(MembershipHistory.valid_from <= end)
        .filter(or_(MembershipHistory.valid_to.is_(None), MembershipHistory.valid_to > start))
        .all()
    )
    return Timeline(
        [(r.kind, r.subject, r.ref_id, r.direction, ts(r.valid_from), ts(r.valid_to)) for r in rows],
        ts(since),
    )
