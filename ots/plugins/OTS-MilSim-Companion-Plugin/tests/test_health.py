"""Giudizi dello stato servizi (tab Manutenzione).

Il punto di questi test non è la raccolta dati — quella parla con RabbitMQ e
col DB — ma il **giudizio**: quando una situazione va chiamata guasto e quando
no. Sbagliare in eccesso è costoso quanto sbagliare in difetto: una spia che
si accende senza motivo smette di essere guardata.
"""

from ots_milsim_companion_plugin import health


# ----------------------------------------------------------------------
# Coda cot_parser: il controllo più importante
# ----------------------------------------------------------------------

def test_nessun_consumer_e_un_errore_e_dice_come_rimediare():
    """È il guasto del 2026-09-22: cot_parser morto, tutto il resto verde."""
    result = health.evaluate_cot_parser(consumers=0, messages=1200, eud_count=2)
    assert result["state"] == health.ERROR
    assert "cot_parser" in result["detail"]
    assert "systemctl restart" in result["detail"]


def test_consumer_presente_e_coda_vuota_e_ok():
    assert health.evaluate_cot_parser(1, 0, 2)["state"] == health.OK


def test_coda_che_si_accumula_e_un_avviso_non_un_errore():
    """Con un consumer attivo i CoT vengono comunque smistati, solo in
    ritardo: è un avviso, non un guasto."""
    result = health.evaluate_cot_parser(1, health.COT_BACKLOG_WARN + 1, 2)
    assert result["state"] == health.WARN


def test_coda_non_interrogabile_e_sconosciuto_non_guasto():
    """Se non si riesce a chiedere, non si inventa una risposta."""
    assert health.evaluate_cot_parser(None, None, 2)["state"] == health.UNKNOWN


def test_nessun_consumer_e_errore_anche_senza_eud_collegati():
    """Il parser deve consumare sempre: senza EUD la coda è vuota, ma un
    consumer a zero resta un guasto perché al primo CoT nessuno lo prenderà."""
    assert health.evaluate_cot_parser(0, 0, 0)["state"] == health.ERROR


# ----------------------------------------------------------------------
# Flusso dei CoT verso il database
# ----------------------------------------------------------------------

def test_cot_recenti_con_eud_collegati_e_ok():
    assert health.evaluate_cot_flow(12, eud_count=2)["state"] == health.OK


def test_cot_fermi_con_eud_collegati_e_un_errore():
    result = health.evaluate_cot_flow(health.COT_STALE_SECONDS + 60, eud_count=2)
    assert result["state"] == health.ERROR
    assert "smistamento fermo" in result["detail"]


def test_cot_fermi_senza_eud_collegati_non_e_un_guasto():
    """Senza nessuno che trasmette la tabella è ferma per forza: segnalarlo
    come guasto sarebbe il falso allarme che fa ignorare la spia."""
    result = health.evaluate_cot_flow(9999, eud_count=0)
    assert result["state"] == health.UNKNOWN
    assert "atteso" in result["detail"]


def test_tabella_vuota_distinta_da_query_fallita():
    """-1 = la query ha risposto, la tabella è vuota. None = non ha risposto."""
    assert health.evaluate_cot_flow(-1, eud_count=2)["state"] == health.WARN
    assert "vuota" in health.evaluate_cot_flow(-1, eud_count=2)["detail"]
    assert health.evaluate_cot_flow(None, eud_count=2)["state"] == health.UNKNOWN


def test_eta_formattata_in_minuti_quando_serve():
    assert "3 min fa" in health.evaluate_cot_flow(200, eud_count=1)["detail"]
    assert "20s fa" in health.evaluate_cot_flow(20, eud_count=1)["detail"]


# ----------------------------------------------------------------------
# EUD collegati
# ----------------------------------------------------------------------

def test_almeno_un_eud_collegato_e_ok():
    euds = [{"connected": True}, {"connected": False}]
    result = health.evaluate_euds(euds)
    assert result["state"] == health.OK
    assert "1 collegati su 2" in result["detail"]


def test_nessun_eud_collegato_e_un_avviso():
    """Non è un errore del server: può semplicemente non esserci nessuno in
    campo. Ma se stai cercando di capire perché non vedi niente, è la prima
    cosa da sapere."""
    assert health.evaluate_euds([{"connected": False}])["state"] == health.WARN


def test_eud_non_verificabili_vengono_contati_a_parte():
    """Coda assente = dispositivo mai collegato, non dispositivo guasto."""
    result = health.evaluate_euds([{"connected": False}, {"connected": None}])
    assert "1 non verificabili" in result["detail"]


def test_nessun_eud_registrato_e_sconosciuto():
    assert health.evaluate_euds([])["state"] == health.UNKNOWN
