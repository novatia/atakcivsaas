# Regole di appartenenza alle squadre.
#
# Assegnare un giocatore a una squadra non è «aggiungerlo a un gruppo»: è uno
# spostamento, e tocca più gruppi insieme. Qui c'è solo la DECISIONE (funzioni
# pure, senza DB e senza RabbitMQ), così è verificabile dai test; l'esecuzione
# — righe in groups_users e binding delle code — sta in app.py.
#
# Le direzioni di OpenTAKServer, che è il motivo per cui serve una regola:
#   OUT  la coda dell'EUD è legata a `<gruppo>.OUT` → l'utente RICEVE
#   IN   i CoT dell'utente sono pubblicati su `<gruppo>.OUT` → l'utente È VISTO
#
# Schema che ne esce per una partita a due squadre con arbitri:
#
#   | chi              | Alpha    | Bravo    | Osservatori |
#   |------------------|----------|----------|-------------|
#   | giocatore Alpha  | IN + OUT | —        | IN          |
#   | giocatore Bravo  | —        | IN + OUT | IN          |
#   | osservatore      | —        | —        | IN + OUT    |
#
# I giocatori pubblicano verso gli osservatori ma non li ricevono: l'arbitro
# vede tutti e resta invisibile. E le due squadre non si vedono fra loro,
# perché nessun giocatore ha OUT sul gruppo osservatori.

IN = "IN"
OUT = "OUT"

# Che tipo di gruppo si sta toccando
KIND_TEAM = "team"
KIND_OBSERVER = "observer"
KIND_PLAIN = "plain"


def roles_from_config(config) -> dict:
    """Ruoli dei gruppi dalla Mappatura Team (0 / lista vuota = non impostato)."""
    observers = config.get("OTS_EVENTCALENDAR_GM_OBSERVER_TEAM_IDS") or []
    return {
        "team_a": int(config.get("OTS_EVENTCALENDAR_GM_TEAM_A_ID") or 0) or None,
        "team_b": int(config.get("OTS_EVENTCALENDAR_GM_TEAM_B_ID") or 0) or None,
        "observers": [int(g) for g in observers if g],
    }


def classify(group_id: int, roles: dict) -> str:
    """Un gruppo può essere mappato sia come squadra sia come osservatori
    (configurazione sbagliata ma possibile): vince la squadra, perché è la
    regola più restrittiva."""
    if group_id in (roles.get("team_a"), roles.get("team_b")):
        return KIND_TEAM
    if group_id in (roles.get("observers") or []):
        return KIND_OBSERVER
    return KIND_PLAIN


def other_team(group_id: int, roles: dict):
    """L'altra squadra, se esiste ed è diversa da questa."""
    if group_id == roles.get("team_a") and roles.get("team_b"):
        return roles["team_b"] if roles["team_b"] != group_id else None
    if group_id == roles.get("team_b") and roles.get("team_a"):
        return roles["team_a"] if roles["team_a"] != group_id else None
    return None


def plan_assign(group_id: int, roles: dict) -> dict:
    """Cosa fare per mettere un utente in questo gruppo.

    `set`   = coppie (group_id, direzione) da garantire presenti
    `clear` = coppie (group_id, direzione | None) da togliere, None = tutte

    Su una squadra è un vero cambio squadra: l'altra squadra viene tolta e
    l'utente comincia a pubblicare verso gli osservatori. Su un gruppo che non
    ha ruoli (visibile solo con «mostra tutti i gruppi») resta l'aggiunta
    semplice di prima: le regole di partita non si applicano a gruppi estranei.
    """
    kind = classify(group_id, roles)
    plan = {"kind": kind, "set": [(group_id, IN), (group_id, OUT)], "clear": []}

    if kind == KIND_TEAM:
        rival = other_team(group_id, roles)
        if rival:
            plan["clear"].append((rival, None))
        for observer_id in roles.get("observers") or []:
            # L'utente pubblica agli osservatori ma non li riceve: niente OUT.
            # Se il gruppo osservatori coincide con la squadra, l'IN c'è già.
            if observer_id != group_id and (observer_id, IN) not in plan["set"]:
                plan["set"].append((observer_id, IN))

    return plan


def plan_remove(group_id: int, roles: dict, current=None) -> dict:
    """Cosa fare per togliere un utente dal gruppo.

    Togliendolo da una squadra resta senza squadra, quindi smette anche di
    pubblicare agli osservatori — ma solo se era lì **come giocatore**.
    `current` è l'insieme delle coppie (group_id, direzione) che l'utente ha
    adesso: se sul gruppo osservatori ha anche OUT allora è un arbitro, e il
    suo IN gli serve per farsi vedere dagli altri arbitri. Senza questo
    controllo un arbitro che scende in campo e poi rientra resterebbe muto.
    """
    kind = classify(group_id, roles)
    current = set(current or ())
    plan = {"kind": kind, "set": [], "clear": [(group_id, None)]}

    if kind == KIND_TEAM:
        for observer_id in roles.get("observers") or []:
            if observer_id == group_id:
                continue
            if (observer_id, OUT) in current:
                continue  # è un osservatore: l'IN è suo, non da giocatore
            plan["clear"].append((observer_id, IN))

    return plan


def describe(plan: dict, names: dict, group_id: int) -> str:
    """Frase per il toast: cosa è cambiato, in italiano."""
    name = names.get(group_id, str(group_id))
    if plan["kind"] != KIND_TEAM:
        return f"aggiunto a {name}"

    removed = [names.get(g, str(g)) for g, d in plan["clear"] if d is None]
    publishes = [names.get(g, str(g)) for g, d in plan["set"] if d == IN and g != group_id]

    parts = [f"assegnato a {name}"]
    if removed:
        parts.append("tolto da " + ", ".join(removed))
    if publishes:
        parts.append("pubblica verso " + ", ".join(publishes))
    return " · ".join(parts)
