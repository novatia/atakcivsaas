# Fork di https://github.com/brian7704/OTS-SkyFi-Plugin (GPL-3.0-or-later).
# Aggiunge la UI (che upstream non distribuisce), il dettaglio ordini e il
# download dei deliverable (image/payload/cog/view-ready) via proxy, dato che
# il browser non può chiamare l'API SkyFi direttamente per via del CORS.
import base64
import mimetypes
import os
import pathlib
import re
import traceback
from urllib.parse import unquote, urlparse
from xml.etree.ElementTree import Element, SubElement, tostring

import requests
import yaml
from flask import (
    Blueprint,
    Flask,
    Response,
    current_app as app,
    jsonify,
    request,
    send_from_directory,
    stream_with_context,
)
from flask_security import roles_accepted

from opentakserver.blueprints.marti_api.data_package_marti_api import create_data_package_zip
from opentakserver.extensions import logger
from opentakserver.plugins.Plugin import Plugin

from .default_config import DefaultConfig

import importlib.metadata

BASE_URL = "https://app.skyfi.com/platform-api"
DELIVERABLE_TYPES = ("image", "payload", "cog", "view-ready")


def _headers() -> dict:
    return {"X-Skyfi-Api-Key": app.config.get("OTS_SKYFI_PLUGIN_API_KEY", "")}


def _get_order(uid: str) -> dict | None:
    r = requests.get(f"{BASE_URL}/orders/{uid}", headers=_headers(), timeout=30)
    return r.json() if r.status_code == 200 else None


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w.\- ]+", "_", name).strip() or "SkyFi"


class SkyFiPlugin(Plugin):
    # Do not change url_prefix
    metadata = pathlib.Path(__file__).resolve().parent.name
    url_prefix = f"/api/plugins/{metadata.lower()}"
    blueprint = Blueprint("SkyFiPlugin", __name__, url_prefix=url_prefix)

    def __init__(self):
        super().__init__()
        self.load_metadata()

    def activate(self, app: Flask, enabled: bool = True):
        self._app = app
        self._load_config()
        self.load_metadata()

        try:
            if not app.config.get("OTS_SKYFI_PLUGIN_API_KEY"):
                logger.warning(f"{self.name}: API key SkyFi non configurata (OTS_SKYFI_PLUGIN_API_KEY)")
            logger.info(f"Successfully Loaded {self.name}")
        except BaseException as e:
            logger.error(f"Failed to load {self.name}: {e}")
            logger.error(traceback.format_exc())

    # Do not change this
    def load_metadata(self):
        try:
            self.distro = pathlib.Path(__file__).resolve().parent.name
            self.metadata = importlib.metadata.metadata(self.distro).json
            self.name = self.metadata["name"]
            self.metadata["distro"] = self.distro
            return self.metadata
        except BaseException as e:
            logger.error(e)
            logger.debug(traceback.format_exc())
            return None

    # Loads default config and user config from ~/ots/config.yml
    def _load_config(self):
        for key in dir(DefaultConfig):
            if key.isupper():
                self._config[key] = getattr(DefaultConfig, key)
                self._app.config.update({key: getattr(DefaultConfig, key)})

        with open(os.path.join(self._app.config.get("OTS_DATA_FOLDER"), "config.yml")) as yaml_file:
            yaml_config = yaml.safe_load(yaml_file)
            for key in self._config.keys():
                value = yaml_config.get(key)
                if value is not None:
                    self._config[key] = value
                    self._app.config.update({key: value})

    def get_info(self):
        self.load_metadata()
        self.get_plugin_routes(self.url_prefix)
        return {"name": self.name, "distro": self.distro, "routes": self.routes}

    def stop(self):
        pass

    # ------------------------------------------------------------------
    # Rotte standard del template (info, UI, config)
    # ------------------------------------------------------------------

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/")
    def plugin_info():
        try:
            distribution = None
            distributions = importlib.metadata.packages_distributions()
            for distro in distributions:
                if str(__name__).startswith(distro):
                    distribution = distributions[distro][0]
                    break

            if distribution:
                info = importlib.metadata.metadata(distribution)
                return jsonify(info.json)
            else:
                return jsonify({"success": False, "error": "Plugin not found"}), 404
        except BaseException as e:
            logger.error(e)
            return jsonify({"success": False, "error": str(e)}), 500

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/ui")
    def ui():
        return send_from_directory(
            f"../{pathlib.Path(__file__).parent.resolve().name}/ui", "index.html", as_attachment=False
        )

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/assets/<file_name>")
    @blueprint.route("/ui/<file_name>")
    def serve(file_name):
        if file_name and os.path.exists(
            os.path.join(pathlib.Path(__file__).parent.resolve(), "ui", "assets", file_name)
        ):
            return send_from_directory(
                f"../{pathlib.Path(__file__).parent.resolve().name}/ui/assets", file_name
            )
        elif file_name and os.path.exists(
            os.path.join(pathlib.Path(__file__).parent.resolve(), "ui", file_name)
        ):
            return send_from_directory(f"../{pathlib.Path(__file__).parent.resolve().name}/ui", file_name)
        else:
            return "", 404

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/config")
    def config():
        config = {}
        for key in dir(DefaultConfig):
            if key.isupper():
                config[key] = app.config.get(key)
        return jsonify(config)

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/config", methods=["POST"])
    def update_config():
        try:
            result = DefaultConfig.update_config(request.json)
            if result["success"]:
                return jsonify(result)
            return jsonify(result), 400
        except BaseException as e:
            logger.error("Failed to update config:" + str(e))
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 400

    # ------------------------------------------------------------------
    # Ordini SkyFi
    # ------------------------------------------------------------------

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/orders", methods=["GET"])
    def get_orders():
        try:
            params = {
                "pageNumber": request.args.get("page", 0),
                "pageSize": request.args.get("page_size", 9),
            }
            if request.args.get("search"):
                params["search"] = request.args.get("search")

            r = requests.get(f"{BASE_URL}/orders", headers=_headers(), params=params, timeout=30)
            if r.status_code == 200:
                return jsonify(r.json())

            logger.error(f"Failed to get orders: {r.text}")
            return jsonify({"success": False, "error": "Controlla l'API key e riprova"}), 400
        except BaseException as e:
            logger.error(f"Failed to get orders: {e}")
            return jsonify({"success": False, "error": f"Failed to get orders: {str(e)}"}), 400

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/orders/<uid>", methods=["GET"])
    def get_order(uid: str):
        try:
            r = requests.get(f"{BASE_URL}/orders/{uid}", headers=_headers(), timeout=30)
            if r.status_code == 200:
                return jsonify(r.json())
            return jsonify({"success": False, "error": f"Ordine non trovato: {r.status_code}"}), r.status_code
        except BaseException as e:
            logger.error(f"Failed to get order {uid}: {e}")
            return jsonify({"success": False, "error": str(e)}), 400

    # Route legacy /<uid>/image mantenuta per compatibilità con l'upstream
    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/orders/<uid>/image")
    @blueprint.route("/<uid>/image")
    def get_preview_image(uid: str):
        r = requests.get(f"{BASE_URL}/orders/{uid}/image", headers=_headers(), timeout=60)
        if r.status_code == 200:
            return f"data:image/png;base64,{base64.b64encode(r.content).decode('UTF-8')}", 200
        return jsonify({"success": False, "error": f"Image download failed with status code {r.status_code}"}), r.status_code

    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/orders/<uid>/download/<deliverable_type>", methods=["GET"])
    def download_deliverable(uid: str, deliverable_type: str):
        """Scarica un deliverable (image/payload/cog/view-ready) facendo da proxy
        verso l'URL firmato di SkyFi, così l'API key non arriva mai al browser."""
        if deliverable_type not in DELIVERABLE_TYPES:
            return jsonify({"success": False, "error": f"Tipo non valido: {deliverable_type}"}), 400

        try:
            order = _get_order(uid) or {}
            r = requests.get(
                f"{BASE_URL}/orders/{uid}/{deliverable_type}",
                headers=_headers(),
                stream=True,
                allow_redirects=True,
                timeout=(10, 300),
            )
            if r.status_code != 200:
                logger.error(f"Deliverable {deliverable_type} for {uid} failed: {r.status_code}")
                return jsonify({"success": False, "error": f"Download fallito: HTTP {r.status_code}"}), r.status_code

            # Nome file: Content-Disposition di SkyFi, altrimenti basename
            # dell'URL firmato, altrimenti ricostruito da ordine + content-type
            filename = None
            disposition = r.headers.get("Content-Disposition", "")
            match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disposition)
            if match:
                filename = os.path.basename(unquote(match.group(1)))
            if not filename:
                url_name = os.path.basename(urlparse(r.url).path)
                if url_name and "." in url_name:
                    filename = unquote(url_name)
            if not filename:
                ext = mimetypes.guess_extension(r.headers.get("Content-Type", "").split(";")[0]) or ""
                filename = f"SkyFi-{order.get('orderCode', uid)}-{deliverable_type}{ext}"

            headers = {
                "Content-Type": r.headers.get("Content-Type", "application/octet-stream"),
                "Content-Disposition": f'attachment; filename="{_safe_name(filename)}"',
            }
            if r.headers.get("Content-Length"):
                headers["Content-Length"] = r.headers["Content-Length"]

            return Response(
                stream_with_context(r.iter_content(chunk_size=64 * 1024)),
                status=200,
                headers=headers,
            )
        except BaseException as e:
            logger.error(f"Failed to download {deliverable_type} for {uid}: {e}")
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500

    # Route legacy /<uid>/data_package mantenuta per compatibilità con l'upstream
    @staticmethod
    @roles_accepted("administrator")
    @blueprint.route("/orders/<uid>/data_package", methods=["POST", "GET"])
    @blueprint.route("/<uid>/data_package", methods=["POST", "GET"])
    def create_data_package(uid: str):
        try:
            order = _get_order(uid)
            if not order:
                return jsonify({"success": False, "error": "Ordine non trovato su SkyFi"}), 404
            if not order.get("tilesUrl"):
                return jsonify({"success": False, "error": "L'ordine non ha ancora i tile WMTS (tilesUrl)"}), 400

            location = order.get("geocodeLocation") or order.get("label") or ""
            package_name = _safe_name(f"SkyFi-{order['orderCode']}_{location}")

            multi_layer_tile_source = Element("customMultiLayerMapSource")
            multi_layer_tile_source.text = f"SkyFi-{order['orderCode']} {location}"

            layers = SubElement(multi_layer_tile_source, "layers")

            google_tiles = SubElement(layers, "customMapSource")
            SubElement(google_tiles, "name").text = "Google Hybrid"
            SubElement(google_tiles, "minZoom").text = "0"
            SubElement(google_tiles, "maxZoom").text = "22"
            SubElement(google_tiles, "tileType").text = "jpg"
            SubElement(google_tiles, "tileUpdate").text = "None"
            SubElement(google_tiles, "url").text = unquote("http://mt1.google.com/vt/lyrs=y&amp;x={$x}&amp;y={$y}&amp;z={$z}")

            skyfi_tiles = SubElement(layers, "customMapSource")
            SubElement(skyfi_tiles, "name").text = f"SkyFi-{order['orderCode']} {location}"
            SubElement(skyfi_tiles, "minZoom").text = "0"
            SubElement(skyfi_tiles, "maxZoom").text = "22"
            SubElement(skyfi_tiles, "tileType").text = "png"
            SubElement(skyfi_tiles, "tileUpdate").text = "None"
            SubElement(skyfi_tiles, "url").text = unquote(
                order["tilesUrl"].replace("{z}", "{$z}").replace("{x}", "{$x}").replace("{y}", "{$y}")
            )

            xml_path = os.path.join(app.config.get("UPLOAD_FOLDER"), f"{package_name}.xml")
            with open(xml_path, "w") as f:
                f.write(tostring(multi_layer_tile_source).decode("UTF-8"))

            create_data_package_zip(xml_path)

            return jsonify({"success": True, "name": package_name}), 200
        except BaseException as e:
            logger.error(f"Failed to create data package for {uid}: {e}")
            logger.error(traceback.format_exc())
            return jsonify({"success": False, "error": str(e)}), 500
