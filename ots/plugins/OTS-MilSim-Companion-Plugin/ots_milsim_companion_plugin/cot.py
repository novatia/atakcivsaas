# Costruzione e broadcast dei CoT di partita.
#
# Il push a TUTTI gli EUD collegati usa gli stessi due exchange RabbitMQ
# dell'endpoint DELETE /api/markers di OTS: "cot_parser" (il server processa e
# salva l'evento) e "firehose" (fanout verso ogni client connesso). I marker
# hanno stale = fine partita, così spariscono da soli dagli ATAK allo scadere
# della durata; la cancellazione anticipata usa il CoT t-x-d-d.
import json
import uuid
from datetime import datetime, timedelta, timezone
from xml.etree.ElementTree import Element, SubElement, tostring

import pika
from flask import current_app as app

from opentakserver.extensions import logger
from opentakserver.functions import iso8601_string_from_datetime

from .game_modes import MARKER_TYPES, ZONE_TYPES


def _base_event(uid: str, cot_type: str, stale: datetime, how: str = "h-g-i-g-o") -> Element:
    now = datetime.now(timezone.utc)
    return Element(
        "event",
        {
            "version": "2.0",
            "uid": uid,
            "type": cot_type,
            "how": how,
            "time": iso8601_string_from_datetime(now),
            "start": iso8601_string_from_datetime(now),
            "stale": iso8601_string_from_datetime(stale),
        },
    )


def _point(event: Element, lat: float, lon: float) -> None:
    SubElement(
        event,
        "point",
        {"lat": str(lat), "lon": str(lon), "hae": "0", "ce": "9999999", "le": "9999999"},
    )


def marker_event(uid: str, marker: dict, stale: datetime, remarks: str = "") -> Element:
    mtype = MARKER_TYPES[marker["type"]]
    event = _base_event(uid, mtype["cot_type"], stale)
    _point(event, marker["lat"], marker["lon"])
    detail = SubElement(event, "detail")
    SubElement(detail, "contact", {"callsign": marker.get("label") or mtype["callsign"]})
    SubElement(detail, "color", {"argb": str(mtype["argb"])})
    if mtype["spot"]:
        # Gli spot marker prendono il colore dall'iconsetpath, non da <color>
        SubElement(detail, "usericon", {"iconsetpath": f"COT_MAPPING_SPOTMAP/b-m-p-s-m/{mtype['argb']}"})
    if remarks:
        SubElement(detail, "remarks").text = remarks
    return event


def zone_event(uid: str, zone: dict, stale: datetime, remarks: str = "") -> Element:
    ztype = ZONE_TYPES[zone["type"]]
    points = zone["points"]
    lat = sum(p[0] for p in points) / len(points)
    lon = sum(p[1] for p in points) / len(points)

    event = _base_event(uid, "u-d-f", stale, how="h-e")
    _point(event, lat, lon)
    detail = SubElement(event, "detail")
    # Poligono chiuso: primo vertice ripetuto in coda
    for p in points + [points[0]]:
        SubElement(detail, "link", {"point": f"{p[0]},{p[1]}"})
    SubElement(detail, "strokeColor", {"value": str(ztype["stroke_argb"])})
    SubElement(detail, "strokeWeight", {"value": "3"})
    SubElement(detail, "fillColor", {"value": str(ztype["fill_argb"])})
    SubElement(detail, "contact", {"callsign": zone.get("label") or ztype["label"]})
    SubElement(detail, "labels_on", {"value": "true"})
    if remarks:
        SubElement(detail, "remarks").text = remarks
    return event


def delete_event(uid: str, cot_type: str) -> Element:
    """t-x-d-d: cancella il marker/area dagli EUD collegati (come DELETE /api/markers)."""
    now = datetime.now(timezone.utc)
    event = _base_event(uid, "t-x-d-d", now + timedelta(minutes=10))
    _point(event, 0, 0)
    detail = SubElement(event, "detail")
    SubElement(detail, "link", {"relation": "p-p", "uid": uid, "type": cot_type})
    return event


def fileshare_event(package: dict, sender_url: str, sender_uid: str, callsign: str) -> Element:
    """b-f-t-r: annuncio data package, ATAK propone il download dal server.

    package: dict serializzato del modello DataPackage di OTS (filename, hash, size).
    """
    now = datetime.now(timezone.utc)
    event = _base_event(f"{sender_uid}.{uuid.uuid4().hex[:8]}", "b-f-t-r", now + timedelta(minutes=10), how="h-e")
    _point(event, 0, 0)
    detail = SubElement(event, "detail")
    SubElement(
        detail,
        "fileshare",
        {
            "filename": package["filename"],
            "name": package["filename"],
            "senderUrl": sender_url,
            "sizeInBytes": str(package.get("size") or 0),
            "sha256": package["hash"],
            "senderUid": sender_uid,
            "senderCallsign": callsign,
            "peerHosted": "false",
        },
    )
    return event


def geochat_event(text: str, sender_uid: str, callsign: str) -> Element:
    """b-t-f su "All Chat Rooms": messaggio in chat generale a tutti gli EUD."""
    now = datetime.now(timezone.utc)
    message_id = str(uuid.uuid4())
    event = _base_event(f"GeoChat.{sender_uid}.All Chat Rooms.{message_id}", "b-t-f", now + timedelta(minutes=10))
    _point(event, 0, 0)
    detail = SubElement(event, "detail")
    chat = SubElement(
        detail,
        "__chat",
        {
            "parent": "RootContactGroup",
            "groupOwner": "false",
            "messageId": message_id,
            "chatroom": "All Chat Rooms",
            "id": "All Chat Rooms",
            "senderCallsign": callsign,
        },
    )
    SubElement(chat, "chatgrp", {"uid0": sender_uid, "uid1": "All Chat Rooms", "id": "All Chat Rooms"})
    SubElement(detail, "link", {"uid": sender_uid, "type": "a-f-G", "relation": "p-p"})
    remarks = SubElement(
        detail,
        "remarks",
        {"source": f"BAO.F.ATAK.{sender_uid}", "to": "All Chat Rooms", "time": iso8601_string_from_datetime(now)},
    )
    remarks.text = text
    return event


def broadcast(events: list[Element]) -> bool:
    """Pubblica gli eventi su cot_parser (persistenza) e firehose (tutti gli EUD).

    Ritorna False se RabbitMQ non è raggiungibile: il chiamante decide se
    considerarlo fatale (Play) o solo un avviso.
    """
    if not events:
        return True
    try:
        credentials = pika.PlainCredentials(
            app.config.get("OTS_RABBITMQ_USERNAME"), app.config.get("OTS_RABBITMQ_PASSWORD")
        )
        connection = pika.BlockingConnection(
            pika.ConnectionParameters(
                host=app.config.get("OTS_RABBITMQ_SERVER_ADDRESS"), credentials=credentials
            )
        )
        channel = connection.channel()
        properties = pika.BasicProperties(expiration=app.config.get("OTS_RABBITMQ_TTL"))
        for event in events:
            body = json.dumps({"cot": tostring(event).decode("utf-8"), "uid": app.config["OTS_NODE_ID"]})
            channel.basic_publish(exchange="cot_parser", routing_key="cot_parser", body=body, properties=properties)
            channel.basic_publish(exchange="firehose", routing_key="", body=body, properties=properties)
        channel.close()
        connection.close()
        return True
    except BaseException as e:
        logger.error(f"MilSim: broadcast CoT fallito: {e}")
        return False
