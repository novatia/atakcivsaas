# Integrazione SkyFi e servizi mappe, ereditata dal fork OTS-SkyFi-Plugin
# (a sua volta fork di https://github.com/brian7704/OTS-SkyFi-Plugin, GPL).
#
# Qui vivono gli helper puri (niente blueprint): chiamate all'API SkyFi via
# proxy (l'API key non arriva mai al browser), risoluzione dei file dei
# contenuti missione, notifica mission change su RabbitMQ e il check del WMS
# del PCN. Le rotte stanno in app.py sul blueprint del plugin.
import json
import mimetypes
import os
import re
from urllib.parse import unquote, urlparse
from xml.etree.ElementTree import tostring

import pika
import requests
from flask import current_app as app
from werkzeug.utils import secure_filename

from opentakserver.extensions import logger
from opentakserver.models.Mission import Mission
from opentakserver.models.MissionChange import MissionChange, generate_mission_change_cot
from opentakserver.models.MissionContent import MissionContent

BASE_URL = "https://app.skyfi.com/platform-api"
DELIVERABLE_TYPES = ("image", "payload", "cog", "view-ready")

# Il WMS del PCN tiene il catalogo (GetCapabilities) vivo anche quando la
# generazione delle immagini è rotta lato Ministero (storage interno
# irraggiungibile): per sapere se le mappe si vedono davvero bisogna chiedere
# una tile GetMap vera, per ogni servizio usato nei data package del gruppo.
PCN_WMS_URL = "http://wms.pcn.minambiente.it/ogc"
PCN_TEST_BBOX = "1076000,5650000,1084000,5658000"  # EPSG:3857, zona Codogno
PCN_SERVICES = [
    {"id": "igm25", "name": "IGM 25.000", "map": "/ms_ogc/WMS_v1.3/raster/IGM_25000.map", "layers": "CB.IGM25000.32,CB.IGM25000.33"},
    {"id": "igm100", "name": "IGM 100.000", "map": "/ms_ogc/WMS_v1.3/raster/IGM_100000.map", "layers": "MB.IGM100000.32,MB.IGM100000.33"},
    {"id": "igm250", "name": "IGM 250.000", "map": "/ms_ogc/WMS_v1.3/raster/IGM_250000.map", "layers": "CB.IGM250000.32,CB.IGM250000.33"},
    {"id": "orto2006", "name": "Ortofoto 2006", "map": "/ms_ogc/WMS_v1.3/raster/ortofoto_colore_06.map", "layers": "OI.ORTOIMMAGINI.2006.32,OI.ORTOIMMAGINI.2006.33"},
    {"id": "orto2012", "name": "Ortofoto 2012", "map": "/ms_ogc/WMS_v1.3/raster/ortofoto_colore_12.map", "layers": "OI.ORTOIMMAGINI.2012"},
]


def check_pcn_service(service: dict) -> dict:
    """GetMap 128x128 con gli stessi parametri degli XML dei data package
    (WMS 1.1.1, EPSG:3857, JPEG): 'online' se torna un'immagine, 'errore' se
    il server risponde con una ServiceException, 'irraggiungibile' se non
    risponde proprio."""
    result = {"id": service["id"], "name": service["name"], "status": "irraggiungibile", "detail": ""}
    try:
        r = requests.get(
            PCN_WMS_URL,
            params={
                "map": service["map"],
                "SERVICE": "WMS", "VERSION": "1.1.1", "REQUEST": "GetMap",
                "LAYERS": service["layers"], "STYLES": "",
                "SRS": "EPSG:3857", "BBOX": PCN_TEST_BBOX,
                "WIDTH": 128, "HEIGHT": 128, "FORMAT": "image/jpeg",
            },
            timeout=(5, 15),
        )
        content_type = r.headers.get("Content-Type", "").lower()
        if r.status_code == 200 and content_type.startswith("image/"):
            result["status"] = "online"
            result["detail"] = f"tile ricevuta ({len(r.content)} byte)"
        else:
            result["status"] = "errore"
            match = re.search(r"<ServiceException>\s*(.*?)\s*</ServiceException>", r.text, re.DOTALL)
            result["detail"] = (match.group(1).strip() if match else f"HTTP {r.status_code}, {content_type}")[:300]
    except BaseException as e:
        result["detail"] = str(e)[:300]
    return result


def headers() -> dict:
    return {"X-Skyfi-Api-Key": app.config.get("OTS_SKYFI_PLUGIN_API_KEY", "")}


def get_order(uid: str) -> dict | None:
    r = requests.get(f"{BASE_URL}/orders/{uid}", headers=headers(), timeout=30)
    return r.json() if r.status_code == 200 else None


def safe_name(name: str) -> str:
    return re.sub(r"[^\w.\- ]+", "_", name).strip() or "SkyFi"


def deliverable_filename(response: requests.Response, order: dict, uid: str, deliverable_type: str) -> str:
    """Nome file del deliverable: Content-Disposition di SkyFi, altrimenti
    basename dell'URL firmato, altrimenti ricostruito da ordine + content-type."""
    disposition = response.headers.get("Content-Disposition", "")
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disposition)
    if match:
        return safe_name(os.path.basename(unquote(match.group(1))))

    url_name = os.path.basename(urlparse(response.url).path)
    if url_name and "." in url_name:
        return safe_name(unquote(url_name))

    ext = mimetypes.guess_extension(response.headers.get("Content-Type", "").split(";")[0]) or ""
    return safe_name(f"SkyFi-{order.get('orderCode', uid)}-{deliverable_type}{ext}")


def mission_content_location(content: MissionContent) -> tuple[str, str] | None:
    """(cartella, nome file) del contenuto su disco, cercando negli stessi due
    posti di /Marti/sync/content: UPLOAD_FOLDER per hash (upload da ATAK/web UI)
    e la cartella missions per nome file (upload di questo plugin)."""
    _, extension = os.path.splitext(secure_filename(content.filename or ""))
    upload_folder = app.config.get("UPLOAD_FOLDER")
    if upload_folder and os.path.exists(os.path.join(upload_folder, f"{content.hash}{extension}")):
        return upload_folder, f"{content.hash}{extension}"
    missions_folder = os.path.join(app.config.get("OTS_DATA_FOLDER"), "missions")
    if content.filename and os.path.exists(os.path.join(missions_folder, content.filename)):
        return missions_folder, content.filename
    return None


def notify_mission_change(mission_name: str, mission: Mission, mission_change: MissionChange, content: MissionContent) -> None:
    """Pubblica il CoT t-x-m-c sull'exchange RabbitMQ `missions` per notificare
    gli EUD iscritti; se il broker non risponde la modifica resta comunque nel
    DB (gli EUD la vedono alla prossima sincronizzazione)."""
    try:
        event = generate_mission_change_cot(mission_name, mission, mission_change, content=content)
        message = json.dumps({"uid": mission_change.creator_uid, "cot": tostring(event).decode("utf-8")})
        rabbit_credentials = pika.PlainCredentials(
            app.config.get("OTS_RABBITMQ_USERNAME"), app.config.get("OTS_RABBITMQ_PASSWORD")
        )
        rabbit_connection = pika.BlockingConnection(
            pika.ConnectionParameters(
                host=app.config.get("OTS_RABBITMQ_SERVER_ADDRESS"),
                credentials=rabbit_credentials,
            )
        )
        channel = rabbit_connection.channel()
        channel.basic_publish("missions", routing_key=f"missions.{mission_name}", body=message)
        channel.close()
        rabbit_connection.close()
    except BaseException as e:
        logger.warning(f"MilSim/SkyFi: modifica missione salvata ma notifica agli EUD fallita: {e}")
