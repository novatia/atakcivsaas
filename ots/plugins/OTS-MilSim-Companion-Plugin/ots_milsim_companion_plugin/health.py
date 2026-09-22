# Stato dei servizi critici di OpenTAKServer, visto da dentro il plugin.
#
# Nato dalla serata del 2026-09-22: cot_parser era morto in silenzio, il
# monitor mostrava traffico (il firehose lo alimenta eud_handler) e nessun EUD
# vedeva più gli altri. Per capirlo sono serviti systemctl, journalctl, psql e
# rabbitmqctl in SSH. Questo modulo mette la stessa diagnosi nella tab
# Manutenzione.
#
# Il plugin gira come utente `ots` dentro il processo web: NON può usare
# systemctl né leggere il journal. Ma non serve — RabbitMQ risponde a domande
# migliori di «l'unit è attiva?»:
#
#   * `queue_declare(passive=True)` sulla coda `cot_parser` restituisce quanti
#     CONSUMER ci sono e quanti messaggi sono in attesa. Zero consumer = nessuno
#     sta smistando i CoT, che è il guasto vero; una unit «active» con il
#     processo morto darebbe comunque zero qui.
#   * la stessa domanda sulla coda di un EUD dice se quel dispositivo è
#     davvero collegato, senza bisogno di `ss` da root.
#
# Le funzioni di valutazione sono pure e testabili a parte: la raccolta dei
# dati sta in `probe_*`, il giudizio in `evaluate`.

import time

OK = "ok"
WARN = "warn"
ERROR = "error"
UNKNOWN = "unknown"

# Oltre questi secondi senza un CoT scritto, con EUD collegati, lo smistamento
# è considerato fermo
COT_STALE_SECONDS = 300
# Messaggi accodati su cot_parser oltre i quali qualcosa non sta consumando
COT_BACKLOG_WARN = 50


def evaluate_cot_parser(consumers, messages, eud_count) -> dict:
    """Giudizio sulla coda `cot_parser`. È il controllo più importante:
    `route_cot()` vive dentro quel processo, quindi se nessuno consuma quella
    coda gli EUD smettono di vedersi fra loro anche se tutto il resto è verde.
    """
    if consumers is None:
        return {"state": UNKNOWN, "detail": "coda cot_parser non interrogabile"}
    if consumers == 0:
        return {
            "state": ERROR,
            "detail": (
                f"nessun consumer sulla coda cot_parser ({messages} messaggi in attesa): "
                "i CoT non vengono smistati ai gruppi né salvati. "
                "Sul server: systemctl restart opentakserver-cot-parser"
            ),
        }
    if messages is not None and messages > COT_BACKLOG_WARN:
        return {
            "state": WARN,
            "detail": f"{consumers} consumer ma {messages} messaggi accodati: lo smistamento è in ritardo",
        }
    return {"state": OK, "detail": f"{consumers} consumer, {messages} messaggi in coda"}


def evaluate_cot_flow(age_seconds, eud_count) -> dict:
    """Ultimo CoT scritto a DB. Senza EUD collegati la tabella è ferma per
    forza: non è un guasto e non va segnalato come tale."""
    if age_seconds is None:
        return {"state": UNKNOWN, "detail": "impossibile leggere la tabella cot"}
    if age_seconds < 0:
        return {"state": WARN if eud_count else UNKNOWN, "detail": "tabella cot vuota"}
    pretty = f"{int(age_seconds)}s fa" if age_seconds < 120 else f"{int(age_seconds / 60)} min fa"
    if not eud_count:
        return {"state": UNKNOWN, "detail": f"ultimo CoT {pretty} (nessun EUD collegato: atteso)"}
    if age_seconds > COT_STALE_SECONDS:
        return {
            "state": ERROR,
            "detail": f"{eud_count} EUD collegati ma l'ultimo CoT è di {pretty}: smistamento fermo",
        }
    return {"state": OK, "detail": f"ultimo CoT {pretty}"}


def evaluate_euds(euds) -> dict:
    """`euds` = lista di dict con `connected` (True/False/None)."""
    known = len(euds)
    connected = sum(1 for e in euds if e.get("connected"))
    unknown = sum(1 for e in euds if e.get("connected") is None)
    if not known:
        return {"state": UNKNOWN, "detail": "nessun EUD registrato"}
    if connected:
        return {"state": OK, "detail": f"{connected} collegati su {known} registrati"}
    return {
        "state": WARN,
        "detail": (
            f"nessuno dei {known} EUD registrati è collegato"
            + (f" ({unknown} non verificabili)" if unknown else "")
        ),
    }


# ----------------------------------------------------------------------
# Raccolta dati
# ----------------------------------------------------------------------


def probe_queues(queue_names: list, config) -> dict:
    """{nome coda: {"consumers": n, "messages": n}} via queue_declare passivo.

    Un canale per coda: se la coda non esiste il broker chiude il canale con
    404 e senza un canale fresco il resto delle domande fallirebbe a cascata.
    Coda inesistente = None, che la UI mostra come «non verificabile» invece
    che come guasto: un EUD che non si è mai collegato non ha una coda.
    """
    import pika

    result = {name: None for name in queue_names}
    connection = None
    try:
        credentials = pika.PlainCredentials(
            config.get("OTS_RABBITMQ_USERNAME"), config.get("OTS_RABBITMQ_PASSWORD")
        )
        connection = pika.BlockingConnection(
            pika.ConnectionParameters(
                host=config.get("OTS_RABBITMQ_SERVER_ADDRESS"),
                credentials=credentials,
                socket_timeout=5,
                blocked_connection_timeout=5,
            )
        )
        for name in queue_names:
            try:
                channel = connection.channel()
                declared = channel.queue_declare(queue=name, passive=True)
                result[name] = {
                    "consumers": declared.method.consumer_count,
                    "messages": declared.method.message_count,
                }
                channel.close()
            except BaseException:
                result[name] = None
        result["_broker"] = True
    except BaseException as e:
        result["_broker"] = False
        result["_error"] = str(e)
    finally:
        try:
            if connection and connection.is_open:
                connection.close()
        except BaseException:
            pass
    return result


def probe_cot_age(db) -> float | None:
    """Secondi dall'ultimo CoT scritto. -1 se la tabella è vuota, None se la
    query non gira (schema diverso, DB non raggiungibile)."""
    from sqlalchemy import text

    try:
        row = db.session.execute(text("SELECT MAX(timestamp) FROM cot")).first()
    except BaseException:
        db.session.rollback()
        return None
    if not row or row[0] is None:
        return -1.0
    import datetime

    last = row[0]
    if isinstance(last, str):
        return None
    # `cot.timestamp` è UTC naive (lo scrive cot_parser da datetime_from_iso8601)
    now = datetime.datetime.utcnow()
    if last.tzinfo is not None:
        now = datetime.datetime.now(datetime.timezone.utc)
    return max(0.0, (now - last).total_seconds())


def report(config, db, mesh_module) -> dict:
    """Il quadro completo per la tab Manutenzione."""
    started = time.time()
    from opentakserver.models.EUD import EUD

    euds = db.session.query(EUD).order_by(EUD.callsign).all()
    # Si interrogano le code degli EUD visti di recente: quelle di dispositivi
    # spariti da mesi non esistono più e allungherebbero il giro per niente
    eud_uids = [e.uid for e in euds][:40]

    probes = probe_queues(["cot_parser"] + eud_uids, config)
    broker_up = probes.get("_broker")

    eud_rows = []
    for eud in euds[:40]:
        probe = probes.get(eud.uid)
        eud_rows.append(
            {
                "uid": eud.uid,
                "callsign": eud.callsign,
                "connected": (probe["consumers"] > 0) if probe else None,
                "pending": probe["messages"] if probe else None,
                "last_event_time": eud.last_event_time.isoformat() + "Z" if eud.last_event_time else None,
            }
        )
    connected_count = sum(1 for r in eud_rows if r["connected"])

    parser = probes.get("cot_parser")
    cot_age = probe_cot_age(db)

    checks = {
        "rabbitmq": (
            {"state": OK, "detail": "raggiungibile"} if broker_up
            else {"state": ERROR, "detail": f"non raggiungibile: {probes.get('_error', '')}"}
        ),
        "cot_parser": (
            evaluate_cot_parser(
                parser["consumers"] if parser else None,
                parser["messages"] if parser else None,
                connected_count,
            ) if broker_up else {"state": UNKNOWN, "detail": "broker non raggiungibile"}
        ),
        "cot_flow": evaluate_cot_flow(cot_age, connected_count),
        "euds": evaluate_euds(eud_rows),
    }

    # I consumer del plugin (firehose, MQTT) li sa già il modulo mesh
    for key, value in (mesh_module.health() or {}).items():
        checks[f"plugin_{key}"] = value

    worst = ERROR if any(c["state"] == ERROR for c in checks.values()) else (
        WARN if any(c["state"] == WARN for c in checks.values()) else OK
    )

    return {
        "overall": worst,
        "checks": checks,
        "euds": eud_rows,
        "connected": connected_count,
        "took_ms": int((time.time() - started) * 1000),
    }
