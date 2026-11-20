"""Tests for the OBS WebSocket switches: stream, recording and virtual camera."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.obs_websocket.const import DOMAIN, REMOVED_BUTTON_KEYS

from .conftest import (
    MOCK_HOST,
    NO_VIRTUAL_CAM_ERROR,
    make_record_status,
    make_stream_status,
    make_virtual_cam_status,
)

STREAM_ID = "switch.obs_studio_192_168_1_100_stream"
RECORD_ID = "switch.obs_studio_192_168_1_100_recording"
VIRTUAL_CAM_ID = "switch.obs_studio_192_168_1_100_virtual_camera"

# entity_id, unique ID suffix, status method, status response factory (active -> response)
SWITCHES = [
    (STREAM_ID, "stream", "get_stream_status", lambda on: make_stream_status(active=on)),
    (RECORD_ID, "record", "get_record_status", lambda on: make_record_status(active=on)),
    (VIRTUAL_CAM_ID, "virtual_camera", "get_virtual_cam_status", lambda on: make_virtual_cam_status(active=on)),
]

# entity_id, service, OBS request, error translation key, error message prefix
COMMANDS = [
    (STREAM_ID, "turn_on", "start_stream", "start_stream_failed", "Failed to start streaming"),
    (STREAM_ID, "turn_off", "stop_stream", "stop_stream_failed", "Failed to stop streaming"),
    (RECORD_ID, "turn_on", "start_record", "start_record_failed", "Failed to start recording"),
    (RECORD_ID, "turn_off", "stop_record", "stop_record_failed", "Failed to stop recording"),
    (VIRTUAL_CAM_ID, "turn_on", "start_virtual_cam", "start_virtual_cam_failed", "Failed to start the virtual camera"),
    (VIRTUAL_CAM_ID, "turn_off", "stop_virtual_cam", "stop_virtual_cam_failed", "Failed to stop the virtual camera"),
]


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _refresh(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()


async def _call(hass: HomeAssistant, service: str, entity_id: str) -> None:
    await hass.services.async_call("switch", service, {"entity_id": entity_id}, blocking=True)
    await hass.async_block_till_done()


@pytest.mark.usefixtures("mock_obsws")
@pytest.mark.parametrize(("entity_id", "suffix", "status_method", "make_status"), SWITCHES)
async def test_switch_follows_obs(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_req_client: MagicMock,
    entity_id: str,
    suffix: str,
    status_method: str,
    make_status,
) -> None:
    """Each switch has a stable unique ID and follows its output's state in OBS."""
    getattr(mock_req_client, status_method).return_value = make_status(True)
    await _setup(hass, mock_config_entry)

    entity = er.async_get(hass).async_get(entity_id)
    assert entity is not None
    assert entity.unique_id == f"{mock_config_entry.entry_id}_{suffix}"
    assert hass.states.get(entity_id).state == STATE_ON

    getattr(mock_req_client, status_method).return_value = make_status(False)
    await _refresh(hass, mock_config_entry)
    assert hass.states.get(entity_id).state == STATE_OFF


@pytest.mark.usefixtures("mock_obsws")
@pytest.mark.parametrize(("entity_id", "service", "method", "translation_key", "message"), COMMANDS)
async def test_switch_sends_request_and_refreshes(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_req_client: MagicMock,
    entity_id: str,
    service: str,
    method: str,
    translation_key: str,
    message: str,
) -> None:
    """Turning a switch on or off sends its request, then refreshes."""
    await _setup(hass, mock_config_entry)
    polls = mock_req_client.get_stream_status.call_count

    await _call(hass, service, entity_id)

    getattr(mock_req_client, method).assert_called_once_with()
    assert mock_req_client.get_stream_status.call_count > polls
    for _entity_id, _service, other, _key, _message in COMMANDS:
        if other != method:
            getattr(mock_req_client, other).assert_not_called()


@pytest.mark.usefixtures("mock_obsws")
@pytest.mark.parametrize(("entity_id", "service", "method", "translation_key", "message"), COMMANDS)
async def test_switch_failure_raises_translated_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_req_client: MagicMock,
    entity_id: str,
    service: str,
    method: str,
    translation_key: str,
    message: str,
) -> None:
    """A failed request raises HomeAssistantError with a translated message."""
    await _setup(hass, mock_config_entry)
    getattr(mock_req_client, method).side_effect = Exception("boom")

    with pytest.raises(HomeAssistantError) as exc_info:
        await _call(hass, service, entity_id)

    assert exc_info.value.translation_key == translation_key
    assert str(exc_info.value).startswith(f"{message} on OBS at {MOCK_HOST}")
    assert str(exc_info.value).endswith(": boom")


@pytest.mark.usefixtures("mock_obsws")
async def test_recording_on_while_paused_resumes(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_req_client: MagicMock
) -> None:
    """A paused recording shows as on, and turning it on resumes it instead of starting a new one."""
    mock_req_client.get_record_status.return_value = make_record_status(active=True, paused=True)
    await _setup(hass, mock_config_entry)
    assert hass.states.get(RECORD_ID).state == STATE_ON

    await _call(hass, "turn_on", RECORD_ID)

    mock_req_client.resume_record.assert_called_once_with()
    mock_req_client.start_record.assert_not_called()


@pytest.mark.usefixtures("mock_obsws")
async def test_stream_on_while_reconnecting(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_req_client: MagicMock
) -> None:
    """A reconnecting stream is still an active output, so the switch stays on."""
    mock_req_client.get_stream_status.return_value = make_stream_status(active=True, reconnecting=True)
    await _setup(hass, mock_config_entry)

    assert hass.states.get(STREAM_ID).state == STATE_ON


@pytest.mark.usefixtures("mock_obsws")
async def test_switches_unavailable_when_obs_unreachable(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_req_client: MagicMock
) -> None:
    """All switches go unavailable with the rest of the device."""
    await _setup(hass, mock_config_entry)

    mock_req_client.get_stream_status.side_effect = ConnectionError("socket closed")
    await _refresh(hass, mock_config_entry)

    for entity_id, *_ in SWITCHES:
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE


@pytest.mark.usefixtures("mock_obsws")
async def test_virtual_cam_unavailable_without_virtual_cam(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_req_client: MagicMock
) -> None:
    """With no virtual camera in OBS that switch is unavailable and OBS is not asked again.

    The other switches and sensors keep working.
    """
    mock_req_client.get_virtual_cam_status.side_effect = NO_VIRTUAL_CAM_ERROR
    await _setup(hass, mock_config_entry)

    assert hass.states.get(VIRTUAL_CAM_ID).state == STATE_UNAVAILABLE
    assert hass.states.get(STREAM_ID).state == STATE_OFF
    assert hass.states.get("sensor.obs_studio_192_168_1_100_stream_status").state == "idle"

    await _refresh(hass, mock_config_entry)
    mock_req_client.get_virtual_cam_status.assert_called_once()

    # A reconnect (OBS restarted, perhaps with a virtual camera now) asks again.
    mock_req_client.get_virtual_cam_status.side_effect = None
    mock_req_client.get_virtual_cam_status.return_value = make_virtual_cam_status(active=True)
    await mock_config_entry.runtime_data.connection.async_disconnect()
    await _refresh(hass, mock_config_entry)
    assert hass.states.get(VIRTUAL_CAM_ID).state == STATE_ON


@pytest.mark.usefixtures("mock_obsws")
async def test_virtual_cam_connection_error_is_not_swallowed(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_req_client: MagicMock
) -> None:
    """Only an OBS request error means "no virtual camera"; a dropped socket still fails the poll."""
    await _setup(hass, mock_config_entry)

    mock_req_client.get_virtual_cam_status.side_effect = ConnectionError("socket closed")
    await _refresh(hass, mock_config_entry)

    assert not mock_config_entry.runtime_data.coordinator.last_update_success
    assert hass.states.get(VIRTUAL_CAM_ID).state == STATE_UNAVAILABLE


@pytest.mark.usefixtures("mock_obsws")
async def test_old_start_stop_buttons_are_removed(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    """Upgrading removes the start/stop buttons the switches replaced, and nothing else."""
    registry = er.async_get(hass)
    for key in REMOVED_BUTTON_KEYS:
        registry.async_get_or_create(
            "button", DOMAIN, f"{mock_config_entry.entry_id}_{key}", config_entry=mock_config_entry
        )
    other = registry.async_get_or_create("button", "other_domain", "unrelated")

    await _setup(hass, mock_config_entry)

    for key in REMOVED_BUTTON_KEYS:
        assert registry.async_get_entity_id("button", DOMAIN, f"{mock_config_entry.entry_id}_{key}") is None
    assert registry.async_get(other.entity_id) is not None
    assert not hass.states.async_entity_ids("button")
