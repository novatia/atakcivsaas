"""Regole di squadra: chi finisce in quale gruppo e con quale direzione.

Lo schema che deve venirne fuori per una partita a due squadre con arbitri:

    | chi              | Alpha    | Bravo    | Osservatori |
    |------------------|----------|----------|-------------|
    | giocatore Alpha  | IN + OUT | —        | IN          |
    | giocatore Bravo  | —        | IN + OUT | IN          |
    | osservatore      | —        | —        | IN + OUT    |

Il punto delicato è che nessun giocatore deve avere OUT sul gruppo
osservatori: se ce l'avesse, Alpha riceverebbe quello che pubblica Bravo e le
due squadre si vedrebbero fra loro.
"""

from ots_milsim_companion_plugin import teams

ALPHA, BRAVO, OBS, LOGISTICA = 1, 2, 3, 7

ROLES = {"team_a": ALPHA, "team_b": BRAVO, "observers": [OBS]}
NAMES = {ALPHA: "Alpha Team", BRAVO: "Bravo Team", OBS: "TacticalScout", LOGISTICA: "Logistica"}


def test_ruoli_dalla_configurazione():
    roles = teams.roles_from_config({
        "OTS_EVENTCALENDAR_GM_TEAM_A_ID": 1,
        "OTS_EVENTCALENDAR_GM_TEAM_B_ID": 2,
        "OTS_EVENTCALENDAR_GM_OBSERVER_TEAM_IDS": [3],
    })
    assert roles == {"team_a": 1, "team_b": 2, "observers": [3]}


def test_ruoli_non_impostati():
    roles = teams.roles_from_config({})
    assert roles == {"team_a": None, "team_b": None, "observers": []}


def test_classificazione_dei_gruppi():
    assert teams.classify(ALPHA, ROLES) == teams.KIND_TEAM
    assert teams.classify(BRAVO, ROLES) == teams.KIND_TEAM
    assert teams.classify(OBS, ROLES) == teams.KIND_OBSERVER
    assert teams.classify(LOGISTICA, ROLES) == teams.KIND_PLAIN


# ----------------------------------------------------------------------
# Assegnazione a una squadra
# ----------------------------------------------------------------------

def test_assegnare_ad_alpha_toglie_da_bravo():
    plan = teams.plan_assign(ALPHA, ROLES)
    assert plan["kind"] == teams.KIND_TEAM
    assert (BRAVO, None) in plan["clear"]          # via da Bravo, tutte le direzioni
    assert (ALPHA, teams.IN) in plan["set"]
    assert (ALPHA, teams.OUT) in plan["set"]


def test_il_giocatore_pubblica_agli_osservatori_ma_non_li_riceve():
    plan = teams.plan_assign(ALPHA, ROLES)
    assert (OBS, teams.IN) in plan["set"]
    assert (OBS, teams.OUT) not in plan["set"], (
        "con OUT sul gruppo osservatori le due squadre si vedrebbero fra loro"
    )


def test_le_due_squadre_non_si_vedono():
    """Verifica d'insieme: nessun giocatore riceve dal gruppo osservatori."""
    for team in (ALPHA, BRAVO):
        plan = teams.plan_assign(team, ROLES)
        riceve = {g for g, d in plan["set"] if d == teams.OUT}
        assert OBS not in riceve
        assert riceve == {team}


def test_assegnare_a_bravo_toglie_da_alpha():
    plan = teams.plan_assign(BRAVO, ROLES)
    assert (ALPHA, None) in plan["clear"]
    assert (BRAVO, teams.OUT) in plan["set"]


def test_piu_gruppi_osservatori():
    roles = {"team_a": ALPHA, "team_b": BRAVO, "observers": [OBS, 8]}
    plan = teams.plan_assign(ALPHA, roles)
    assert (OBS, teams.IN) in plan["set"]
    assert (8, teams.IN) in plan["set"]


def test_senza_altra_squadra_configurata_non_si_toglie_niente():
    roles = {"team_a": ALPHA, "team_b": None, "observers": [OBS]}
    plan = teams.plan_assign(ALPHA, roles)
    assert plan["clear"] == []
    assert (OBS, teams.IN) in plan["set"]


def test_senza_osservatori_configurati():
    roles = {"team_a": ALPHA, "team_b": BRAVO, "observers": []}
    plan = teams.plan_assign(ALPHA, roles)
    assert plan["set"] == [(ALPHA, teams.IN), (ALPHA, teams.OUT)]
    assert plan["clear"] == [(BRAVO, None)]


# ----------------------------------------------------------------------
# Osservatori e gruppi senza ruolo
# ----------------------------------------------------------------------

def test_osservatore_riceve_e_vede_gli_altri_osservatori():
    plan = teams.plan_assign(OBS, ROLES)
    assert plan["kind"] == teams.KIND_OBSERVER
    assert set(plan["set"]) == {(OBS, teams.IN), (OBS, teams.OUT)}
    # Entrare fra gli osservatori non tocca le squadre
    assert plan["clear"] == []


def test_gruppo_senza_ruolo_resta_aggiunta_semplice():
    plan = teams.plan_assign(LOGISTICA, ROLES)
    assert plan["kind"] == teams.KIND_PLAIN
    assert set(plan["set"]) == {(LOGISTICA, teams.IN), (LOGISTICA, teams.OUT)}
    assert plan["clear"] == []


def test_gruppo_mappato_sia_squadra_sia_osservatori():
    """Configurazione sbagliata ma possibile: vince la regola di squadra e non
    si generano coppie duplicate."""
    roles = {"team_a": ALPHA, "team_b": BRAVO, "observers": [ALPHA]}
    plan = teams.plan_assign(ALPHA, roles)
    assert plan["kind"] == teams.KIND_TEAM
    assert plan["set"].count((ALPHA, teams.IN)) == 1


# ----------------------------------------------------------------------
# Rimozione
# ----------------------------------------------------------------------

def test_togliere_da_una_squadra_toglie_anche_la_pubblicazione_agli_osservatori():
    plan = teams.plan_remove(ALPHA, ROLES)
    assert (ALPHA, None) in plan["clear"]
    assert (OBS, teams.IN) in plan["clear"]


def test_togliere_da_una_squadra_non_tocca_lout_di_un_osservatore():
    """Se l'utente è anche arbitro, il suo OUT sul gruppo osservatori resta."""
    plan = teams.plan_remove(ALPHA, ROLES)
    assert (OBS, None) not in plan["clear"]
    assert (OBS, teams.OUT) not in plan["clear"]


def test_togliere_dagli_osservatori_non_tocca_le_squadre():
    plan = teams.plan_remove(OBS, ROLES)
    assert plan["clear"] == [(OBS, None)]


def test_togliere_da_un_gruppo_senza_ruolo():
    plan = teams.plan_remove(LOGISTICA, ROLES)
    assert plan["clear"] == [(LOGISTICA, None)]


# ----------------------------------------------------------------------
# Descrizione per la UI
# ----------------------------------------------------------------------

def test_descrizione_del_cambio_squadra():
    plan = teams.plan_assign(ALPHA, ROLES)
    testo = teams.describe(plan, NAMES, ALPHA)
    assert "assegnato a Alpha Team" in testo
    assert "tolto da Bravo Team" in testo
    assert "pubblica verso TacticalScout" in testo


def test_descrizione_di_un_gruppo_semplice():
    plan = teams.plan_assign(LOGISTICA, ROLES)
    assert teams.describe(plan, NAMES, LOGISTICA) == "aggiunto a Logistica"


# ----------------------------------------------------------------------
# Scenario completo
# ----------------------------------------------------------------------

def apply(state: dict, user: str, plan: dict) -> None:
    """Applica un piano a uno stato {utente: {(group_id, direzione)}}."""
    memberships = state.setdefault(user, set())
    for group_id, direction in plan["clear"]:
        for d in ([direction] if direction else [teams.IN, teams.OUT]):
            memberships.discard((group_id, d))
    for pair in plan["set"]:
        memberships.add(pair)


def test_scenario_partita_completo():
    state = {}
    apply(state, "rossi", teams.plan_assign(ALPHA, ROLES))
    apply(state, "bianchi", teams.plan_assign(BRAVO, ROLES))
    apply(state, "arbitro", teams.plan_assign(OBS, ROLES))

    assert state["rossi"] == {(ALPHA, teams.IN), (ALPHA, teams.OUT), (OBS, teams.IN)}
    assert state["bianchi"] == {(BRAVO, teams.IN), (BRAVO, teams.OUT), (OBS, teams.IN)}
    assert state["arbitro"] == {(OBS, teams.IN), (OBS, teams.OUT)}

    # L'arbitro riceve da OBS, dove entrambe le squadre pubblicano
    riceve_arbitro = {g for g, d in state["arbitro"] if d == teams.OUT}
    pubblica_rossi = {g for g, d in state["rossi"] if d == teams.IN}
    pubblica_bianchi = {g for g, d in state["bianchi"] if d == teams.IN}
    assert riceve_arbitro & pubblica_rossi == {OBS}
    assert riceve_arbitro & pubblica_bianchi == {OBS}

    # Le squadre non si vedono fra loro
    riceve_rossi = {g for g, d in state["rossi"] if d == teams.OUT}
    assert riceve_rossi & pubblica_bianchi == set()

    # Nessuno vede l'arbitro
    pubblica_arbitro = {g for g, d in state["arbitro"] if d == teams.IN}
    assert riceve_rossi & pubblica_arbitro == set()


def test_cambio_squadra_a_partita_iniziata():
    state = {}
    apply(state, "rossi", teams.plan_assign(ALPHA, ROLES))
    apply(state, "rossi", teams.plan_assign(BRAVO, ROLES))
    assert state["rossi"] == {(BRAVO, teams.IN), (BRAVO, teams.OUT), (OBS, teams.IN)}
    assert not any(g == ALPHA for g, _ in state["rossi"])


def test_uscita_dalla_squadra_lascia_senza_gruppi():
    state = {}
    apply(state, "rossi", teams.plan_assign(ALPHA, ROLES))
    apply(state, "rossi", teams.plan_remove(ALPHA, ROLES, state["rossi"]))
    assert state["rossi"] == set()


def test_arbitro_che_scende_in_campo_resta_arbitro():
    """Un osservatore assegnato a una squadra tiene il suo OUT sugli
    osservatori: continua a vedere tutti."""
    state = {}
    apply(state, "arbitro", teams.plan_assign(OBS, ROLES))
    apply(state, "arbitro", teams.plan_assign(ALPHA, ROLES))
    assert (OBS, teams.OUT) in state["arbitro"]
    assert (ALPHA, teams.OUT) in state["arbitro"]

    # E togliendolo dalla squadra torna solo arbitro
    apply(state, "arbitro", teams.plan_remove(ALPHA, ROLES, state["arbitro"]))
    assert state["arbitro"] == {(OBS, teams.IN), (OBS, teams.OUT)}
