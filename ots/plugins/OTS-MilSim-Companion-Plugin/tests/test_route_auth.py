"""Ogni rotta del blueprint deve avere il controllo di accesso EFFICACE.

I decoratori si applicano dal basso verso l'alto: `@blueprint.route` registra
in Flask la funzione che riceve in quel momento. Se `@roles_accepted` o
`@auth_required` stanno SOPRA la route, Flask ha già registrato la funzione
nuda e il controllo non gira mai: fino alla 3.22.1 82 rotte su 94 (config con
la chiave SkyFi compresa) rispondevano a chiunque senza login.

Ordine giusto:

    @staticmethod
    @blueprint.route("/config")
    @roles_accepted("administrator")
    def config(): ...
"""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "ots_milsim_companion_plugin" / "app.py"
AUTH = ("roles_accepted", "roles_required", "auth_required", "login_required")
# Rotte pubbliche di proposito: la pagina della UI e i suoi file statici
PUBLIC = {"ui", "serve"}


def _routes():
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        names = [ast.unparse(d) for d in node.decorator_list]
        route = [i for i, n in enumerate(names) if n.startswith("blueprint.route")]
        if route:
            auth = [i for i, n in enumerate(names) if n.startswith(AUTH)]
            yield node.name, route[0], auth


def test_il_controllo_di_accesso_sta_sotto_la_route():
    ignored = [name for name, route, auth in _routes() if auth and auth[0] < route]
    assert not ignored, f"controllo di accesso ignorato (decoratore sopra @blueprint.route): {ignored}"


def test_nessuna_rotta_senza_controllo_oltre_la_ui():
    unprotected = {name for name, _, auth in _routes() if not auth} - PUBLIC
    assert not unprotected, f"rotte senza controllo di accesso: {sorted(unprotected)}"
