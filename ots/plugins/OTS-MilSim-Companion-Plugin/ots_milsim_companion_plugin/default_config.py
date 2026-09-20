import os
import traceback
from dataclasses import dataclass

import yaml
from flask import current_app as app

from opentakserver.extensions import logger


@dataclass
class DefaultConfig:
    # Caricato per primo, poi sovrascritto dai valori utente in ~/ots/config.yml.
    # Il prefisso OTS_EVENTCALENDAR_ è storico (il plugin nasce come calendario):
    # le chiavi restano invariate per non rompere i config.yml esistenti.
    OTS_EVENTCALENDAR_PLUGIN_ENABLED = True
    # Punti assegnati all'operatore per ogni presenza confermata dall'amministratore
    OTS_EVENTCALENDAR_POINTS_PER_PRESENCE = 10
    # Fuso orario degli orari inseriti nel calendario (i punti CoT sono in UTC):
    # serve al replay giocata per pescare la finestra giusta anche se il server e' in UTC
    OTS_EVENTCALENDAR_TIMEZONE = "Europe/Rome"
    # Callsign con cui il Game Master firma marker, chat e invii data package al Play
    OTS_EVENTCALENDAR_GM_CALLSIGN = "Game Master"
    # Hostname/IP che gli EUD usano per scaricare i data package annunciati al Play
    # (senderUrl del fileshare). Vuoto = host con cui l'admin sta aprendo la web UI.
    OTS_EVENTCALENDAR_GM_SERVER_ADDRESS = ""
    # API key SkyFi (app.skyfi.com → Profile → API Key): stessa chiave del vecchio
    # OTS-SkyFi-Plugin, così i config.yml esistenti continuano a funzionare
    OTS_SKYFI_PLUGIN_API_KEY = ""
    # Mappatura di default dei team ATAK (id della tabella teams di OTS),
    # configurata dalla tab Team e precompilata nel pannello del Play.
    # 0 = non impostato (broadcast a tutti se non si sceglie nulla al Play).
    OTS_EVENTCALENDAR_GM_TEAM_A_ID = 0
    OTS_EVENTCALENDAR_GM_TEAM_B_ID = 0
    OTS_EVENTCALENDAR_GM_OBSERVER_TEAM_IDS = []

    @staticmethod
    def validate(config: dict) -> dict:
        try:
            for key, value in config.items():
                if key not in DefaultConfig.__dict__.keys():
                    return {"success": False, "error": f"{key} is not a valid config key"}
                if key == "OTS_EVENTCALENDAR_PLUGIN_ENABLED" and not isinstance(value, bool):
                    return {"success": False, "error": f"{key} should be a boolean"}
                if key == "OTS_EVENTCALENDAR_POINTS_PER_PRESENCE" and (not isinstance(value, int) or value < 0):
                    return {"success": False, "error": f"{key} should be a non-negative integer"}
                if key == "OTS_EVENTCALENDAR_TIMEZONE":
                    from zoneinfo import ZoneInfo

                    try:
                        ZoneInfo(str(value))
                    except BaseException:
                        return {"success": False, "error": f"{value} is not a valid IANA timezone"}
                if key in ("OTS_EVENTCALENDAR_GM_CALLSIGN", "OTS_EVENTCALENDAR_GM_SERVER_ADDRESS", "OTS_SKYFI_PLUGIN_API_KEY") and not isinstance(value, str):
                    return {"success": False, "error": f"{key} should be a string"}
                if key in ("OTS_EVENTCALENDAR_GM_TEAM_A_ID", "OTS_EVENTCALENDAR_GM_TEAM_B_ID") and (not isinstance(value, int) or value < 0):
                    return {"success": False, "error": f"{key} should be a non-negative integer (0 = non impostato)"}
                if key == "OTS_EVENTCALENDAR_GM_OBSERVER_TEAM_IDS" and (
                    not isinstance(value, list) or not all(isinstance(v, int) and v > 0 for v in value)
                ):
                    return {"success": False, "error": f"{key} should be a list of team ids"}

            return {"success": True, "error": ""}
        except BaseException as e:
            logger.error(traceback.format_exc())
            return {"success": False, "error": str(e)}

    @staticmethod
    def save_config_settings(settings: dict):
        try:
            with open(os.path.join(app.config.get("OTS_DATA_FOLDER"), "config.yml"), "r") as config_file:
                config = yaml.safe_load(config_file.read())

            for setting, value in settings.items():
                config[setting] = value
                app.config.update({setting: value})

            with open(os.path.join(app.config.get("OTS_DATA_FOLDER"), "config.yml"), "w") as config_file:
                yaml.safe_dump(config, config_file)
        except BaseException as e:
            logger.error(f"Failed to save settings {settings}: {e}")

    @staticmethod
    def update_config(config: dict) -> dict:
        try:
            valid = DefaultConfig.validate(config)
            if valid["success"]:
                DefaultConfig.save_config_settings(config)
                return {"success": True}
            else:
                return valid
        except BaseException as e:
            logger.error(f"Failed to update config: {e}")
            logger.error(traceback.format_exc())
            return {"success": False, "error": str(e)}
