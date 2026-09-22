"""Stub minimi per far girare i test senza installare OpenTAKServer.

Il modulo `mesh` dipende da `pika`, da `flask.current_app` e da
`opentakserver.extensions`. In un venv OTS reale ci sono tutti; qui si
sostituiscono con stub leggeri quando mancano, così la suite gira ovunque
(CI, portatile, server) e testa la logica del plugin, non le librerie.

L'accesso al DB di `mesh` è concentrato in poche funzioni
(`load_mappings`, `_group_name`, `eud_groups`, `load_overrides`,
`persist_tag`): i test le sostituiscono con monkeypatch invece di montare
un database finto.
"""

import sys
import types

import pytest


def _ensure(name: str, factory):
    if name in sys.modules:
        return sys.modules[name]
    try:
        return __import__(name, fromlist=["*"])
    except BaseException:
        module = factory()
        sys.modules[name] = module
        return module


def _pika_stub():
    module = types.ModuleType("pika")

    class BasicProperties:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class PlainCredentials:
        def __init__(self, *args, **kwargs):
            pass

    class ConnectionParameters:
        def __init__(self, *args, **kwargs):
            pass

    class BlockingConnection:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("RabbitMQ non disponibile nei test")

    module.BasicProperties = BasicProperties
    module.PlainCredentials = PlainCredentials
    module.ConnectionParameters = ConnectionParameters
    module.BlockingConnection = BlockingConnection
    return module


def _flask_stub():
    module = types.ModuleType("flask")

    class _NoAppConfig:
        def get(self, *args, **kwargs):
            raise RuntimeError("Working outside of application context")

    class _CurrentApp:
        config = _NoAppConfig()

    module.current_app = _CurrentApp()
    return module


def _opentakserver_stub():
    root = types.ModuleType("opentakserver")
    root.__path__ = []
    extensions = types.ModuleType("opentakserver.extensions")

    class _Session:
        def query(self, *args, **kwargs):
            raise RuntimeError("DB non disponibile nei test: usare monkeypatch")

        def get(self, *args, **kwargs):
            raise RuntimeError("DB non disponibile nei test: usare monkeypatch")

        def commit(self):
            pass

        def rollback(self):
            pass

        def add(self, *args):
            pass

    class _Db:
        session = _Session()

    import logging

    extensions.db = _Db()
    extensions.logger = logging.getLogger("milsim-tests")
    sys.modules["opentakserver"] = root
    sys.modules["opentakserver.extensions"] = extensions
    return root


_ensure("pika", _pika_stub)
_ensure("flask", _flask_stub)
try:
    __import__("opentakserver.extensions")
except BaseException:
    _opentakserver_stub()

import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ots_milsim_companion_plugin import mesh  # noqa: E402


class Mapping:
    """Sostituto di MeshChannelMap: `match_mapping` legge solo attributi."""

    def __init__(self, group_id, channel_name=None, channel_index=None, enabled=True):
        self.group_id = group_id
        self.channel_name = channel_name
        self.channel_index = channel_index
        self.enabled = enabled


class FakeChannel:
    """Canale RabbitMQ finto: registra le pubblicazioni invece di inviarle."""

    def __init__(self, fail=False):
        self.published = []
        self.fail = fail

    def basic_publish(self, exchange, routing_key, body, properties=None):
        if self.fail:
            raise RuntimeError("broker irraggiungibile")
        self.published.append({"exchange": exchange, "routing_key": routing_key, "body": body})


@pytest.fixture(autouse=True)
def clean_registry():
    """Ogni test parte da un registry vuoto: lo stato vivo è globale."""
    mesh.REGISTRY.tags.clear()
    mesh.REGISTRY.aliases.clear()
    mesh.REGISTRY.events.clear()
    mesh.REGISTRY.rx_times.clear()
    mesh.REGISTRY.node_channels.clear()
    mesh.REGISTRY.unknown_channel_keys.clear()
    mesh.REGISTRY.routing_errors = 0
    mesh.REGISTRY.seq = 0
    # La cache TTL di mappature/override/gruppi è globale: senza pulizia un
    # test si porterebbe dietro la configurazione del precedente
    mesh.invalidate_cache()
    yield


@pytest.fixture
def env(monkeypatch):
    """Ambiente controllato: gruppi, mappature, override e configurazione.

    `groups` è la tabella `groups` di OTS; `eud_groups` simula
    `route_cot()`, cioè i gruppi dell'utente che possiede l'EUD relay.
    """

    state = {
        "groups": {1: "ALPHA", 2: "BRAVO", 3: "LOGISTICS", 9: "Meshtastic"},
        "mappings": [],
        "overrides": {},
        "eud_groups": {},
        "config": {
            "OTS_MILSIM_MESH_FALLBACK_POLICY": "source_eud_group",
            "OTS_MILSIM_MESH_DEFAULT_GROUP_ID": 0,
            "OTS_MESHTASTIC_GROUP": "Meshtastic",
            "OTS_RABBITMQ_TTL": "86400000",
            "OTS_MILSIM_MESH_LIVE_SECONDS": 60,
            "OTS_MILSIM_MESH_RECENT_SECONDS": 300,
            "OTS_MILSIM_MESH_GPS_STALE_SECONDS": 120,
        },
    }

    monkeypatch.setattr(mesh, "load_mappings", lambda: state["mappings"])
    monkeypatch.setattr(mesh, "load_overrides", lambda: state["overrides"])
    monkeypatch.setattr(mesh, "persist_tag", lambda tag, force=False: None)
    monkeypatch.setattr(mesh, "_group_name", lambda gid: state["groups"].get(int(gid)))
    monkeypatch.setattr(
        mesh, "_group_id_by_name",
        lambda name: next((gid for gid, n in state["groups"].items() if n == name), None),
    )
    monkeypatch.setattr(
        mesh, "eud_groups",
        lambda uid: [
            {"id": gid, "name": state["groups"][gid]} for gid in state["eud_groups"].get(uid, [])
        ],
    )
    monkeypatch.setattr(mesh, "_cfg", lambda key, default=None: state["config"].get(key, default))
    return state
