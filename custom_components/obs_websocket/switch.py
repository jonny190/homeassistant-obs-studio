"""Switch platform for OBS WebSocket: stream, recording and virtual camera."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import OBSConfigEntry, OBSConnection, OBSCoordinator, bool_field
from .const import DOMAIN

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class OBSSwitchEntityDescription(SwitchEntityDescription):
    """Describes an OBS output switch: its state, its requests and their error messages.

    `is_on_fn` returning None means the output is not available (the entity
    shows as unavailable). `turn_on_fn` gets the coordinator data so it can
    pick the right request for the current state.
    """

    is_on_fn: Callable[[dict[str, Any]], bool | None]
    turn_on_fn: Callable[[OBSConnection, dict[str, Any]], Awaitable[None]]
    turn_off_fn: Callable[[OBSConnection], Awaitable[None]]
    turn_on_error_key: str
    turn_off_error_key: str


def _record_turn_on(conn: OBSConnection, data: dict[str, Any]) -> Awaitable[None]:
    # A paused recording is still "on"; turning it on again resumes it rather
    # than failing with "output already active".
    if bool_field(data.get("record_status"), "output_paused"):
        return conn.async_resume_record()
    return conn.async_start_record()


SWITCHES: tuple[OBSSwitchEntityDescription, ...] = (
    OBSSwitchEntityDescription(
        key="stream",
        translation_key="stream",
        is_on_fn=lambda data: bool_field(data.get("stream_status"), "output_active"),
        turn_on_fn=lambda conn, data: conn.async_start_stream(),
        turn_off_fn=lambda conn: conn.async_stop_stream(),
        turn_on_error_key="start_stream_failed",
        turn_off_error_key="stop_stream_failed",
    ),
    OBSSwitchEntityDescription(
        key="record",
        translation_key="record",
        is_on_fn=lambda data: bool_field(data.get("record_status"), "output_active"),
        turn_on_fn=_record_turn_on,
        turn_off_fn=lambda conn: conn.async_stop_record(),
        turn_on_error_key="start_record_failed",
        turn_off_error_key="stop_record_failed",
    ),
    OBSSwitchEntityDescription(
        key="virtual_camera",
        translation_key="virtual_camera",
        # None when OBS has no virtual camera (Linux without v4l2loopback).
        is_on_fn=lambda data: data.get("virtual_cam"),
        turn_on_fn=lambda conn, data: conn.async_start_virtual_cam(),
        turn_off_fn=lambda conn: conn.async_stop_virtual_cam(),
        turn_on_error_key="start_virtual_cam_failed",
        turn_off_error_key="stop_virtual_cam_failed",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: OBSConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up OBS switches from a config entry."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(OBSSwitch(coordinator, entry, description) for description in SWITCHES)


class OBSSwitch(CoordinatorEntity[OBSCoordinator], SwitchEntity):
    """Starts and stops one OBS output, and shows whether it is running.

    The state follows OBS, so it also changes when the output is started or
    stopped from OBS itself.
    """

    _attr_has_entity_name = True
    entity_description: OBSSwitchEntityDescription

    def __init__(
        self,
        coordinator: OBSCoordinator,
        entry: OBSConfigEntry,
        description: OBSSwitchEntityDescription,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"OBS Studio ({entry.data['host']})",
            manufacturer="OBS Project",
            sw_version=None,
        )

    @property
    def available(self) -> bool:
        """Return False when OBS is unreachable or does not have this output."""
        return super().available and self.is_on is not None

    @property
    def is_on(self) -> bool | None:
        """Return whether the output is running."""
        return self.entity_description.is_on_fn(self.coordinator.data or {})

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start the output."""
        connection = self.coordinator.connection
        await self._async_send(
            self.entity_description.turn_on_fn(connection, self.coordinator.data or {}),
            self.entity_description.turn_on_error_key,
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop the output."""
        await self._async_send(
            self.entity_description.turn_off_fn(self.coordinator.connection),
            self.entity_description.turn_off_error_key,
        )

    async def _async_send(self, request: Awaitable[None], error_key: str) -> None:
        connection = self.coordinator.connection
        try:
            await request
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=error_key,
                translation_placeholders={"host": connection.host, "error": str(err)},
            ) from err
        # OBS also sends a state-changed event; this covers a missed one.
        await self.coordinator.async_request_refresh()
