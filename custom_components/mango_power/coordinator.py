"""Polling coordinator: one per Mango Power unit."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import MangoApi, MangoAuthError, MangoError
from .const import DOMAIN, MODEL_E, MODEL_S, SCAN_INTERVAL, SUPPORTED_MODELS

_LOGGER = logging.getLogger(__name__)


class MangoDeviceCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches realtime data, device state and settings for one unit."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, api: MangoApi, device: dict[str, Any]
    ) -> None:
        self.api = api
        self.device_id: str = str(device["id"])
        self.sn: str = str(device.get("sn") or device.get("displaySn") or self.device_id)
        self.model: str = device.get("model") or MODEL_E
        self.model_name: str = SUPPORTED_MODELS.get(self.model, "Mango Power")
        self.group_name: str | None = device.get("group_name")
        self.is_group_root: bool = bool(device.get("is_group_root"))
        name = device.get("name")
        if self.model == MODEL_S:
            # Inverters are often just named "1", "2 (left)"... in the app; make them identifiable.
            self.device_name: str = f"{self.model_name} {name}" if name else f"{self.model_name} {self.sn[-3:]}"
        else:
            self.device_name = name or f"{self.model_name} {self.sn[-3:]}"
        self.firmware: str | None = None
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{self.sn}",
            update_interval=SCAN_INTERVAL,
        )

    @property
    def tz(self) -> str:
        return self.hass.config.time_zone or "UTC"

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            rt = await self.api.realtime(self.device_id, self.tz)
            info = await self.api.device_info(self.device_id)
            # The S exposes no settings (the cloud returns an empty object), so skip that call.
            setting = await self.api.settings(self.device_id, self.tz) if self.model == MODEL_E else {}
        except MangoAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except MangoError as err:
            raise UpdateFailed(str(err)) from err

        state = info.get("state") or {}
        self.firmware = state.get("firmwareVersion") or self.firmware
        common = {
            **{k: v for k, v in rt.items() if not isinstance(v, (dict, list))},
            "online": state.get("status") == 1,
            "lastActiveAt": state.get("lastActiveAt"),
            "firmware": self.firmware,
        }
        if self.model == MODEL_S:
            # Only the mesh level is taken: the rest of meshNetwork holds Wi-Fi credentials.
            return {**common, "meshLevel": (state.get("meshNetwork") or {}).get("meshLevel")}

        ems = setting.get("emsSetting") or {}
        pcs = setting.get("pcsSetting") or {}
        return {
            **common,
            "soc": state.get("soc"),
            **{k: ems.get(k) for k in (
                "acOutputEnable", "dcOutputEnable", "chargeEnable", "upsEnable",
                "maxChargeCurrent", "upsAutoChargeSoc",
                # not exposed by the app; read-only diagnostics
                "discharge240Enable", "timerMode", "ecoReservedSoc", "boot",
            )},
            "acOutputVolt": pcs.get("acOutputVolt"),
            "acFrequency": pcs.get("acFrequency"),
        }

    async def async_send(self, setting_type: str, setting_data: dict[str, Any]) -> None:
        """Send a control command, then refresh shortly after."""
        try:
            await self.api.send_cmd(self.device_id, setting_type, setting_data)
        except MangoAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except MangoError as err:
            raise HomeAssistantError(f"Mango Power command failed: {err}") from err
