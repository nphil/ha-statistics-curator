"""Smoke tests against a real Home Assistant core and recorder.

The decision logic is covered by test_logic.py without Home Assistant. These
check what that cannot: that every recorder, registry and flow API the
integration imports still exists in the installed core, and that the entry
sets up, shows its options and unloads. They run in CI against stable and beta
Home Assistant, which is what warns before a release breaks the integration.
"""

from __future__ import annotations

from pytest_homeassistant.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.statistics_curator import DOMAIN


async def test_config_flow_confirms_then_creates_the_entry(
    recorder_mock, hass: HomeAssistant
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    (entry,) = hass.config_entries.async_entries(DOMAIN)
    assert entry.state is ConfigEntryState.LOADED


async def test_entry_loads_registers_the_service_and_unloads(
    recorder_mock, hass: HomeAssistant
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Statistics Curator")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert hass.services.has_service(DOMAIN, "curate")

    # A pass over a recorder with nothing to settle exercises validate_statistics
    # and get_metadata for real.
    await hass.services.async_call(DOMAIN, "curate", {}, blocking=True)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not hass.services.has_service(DOMAIN, "curate")


async def test_options_flow_shows_the_policy_form(
    recorder_mock, hass: HomeAssistant
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, title="Statistics Curator")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
