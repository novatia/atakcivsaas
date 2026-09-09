import os
import traceback
from dataclasses import dataclass

import yaml
from flask import current_app as app

from opentakserver.extensions import logger


@dataclass
class DefaultConfig:
    # Caricato per primo, poi sovrascritto dai valori utente in ~/ots/config.yml
    OTS_EVENTCALENDAR_PLUGIN_ENABLED = True
    # Punti assegnati all'operatore per ogni presenza confermata dall'amministratore
    OTS_EVENTCALENDAR_POINTS_PER_PRESENCE = 10

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
