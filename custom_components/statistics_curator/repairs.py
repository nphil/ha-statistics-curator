"""Fix flow for the reused-id repair.

The mixed series can only be settled by clearing it. Offering that as the
repair's Fix button keeps the operator on one screen and lets the curator
retire the record and the issue in the same step, instead of waiting for
the periodic prune to notice a clear done from the Statistics page.
"""

from __future__ import annotations

from typing import Any, cast

import voluptuous as vol

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from . import DOMAIN, REUSED_ISSUE_PREFIX, Curator, StatisticsCuratorConfigEntry


def _loaded_curator(hass: HomeAssistant) -> Curator | None:
    """Return the active entry's curator, if its repair can be safely handled."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED:
            return cast(StatisticsCuratorConfigEntry, entry).runtime_data
    return None


class ReusedIdFixFlow(RepairsFlow):
    """Confirm, then clear the mixed statistics."""

    def __init__(self, statistic_id: str) -> None:
        self.statistic_id = statistic_id

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        if user_input is not None:
            curator = _loaded_curator(self.hass)
            if curator is None:
                return self.async_abort(reason="not_loaded")
            await curator.async_clear_and_forget(self.statistic_id)
            return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={"statistic_id": self.statistic_id},
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    return ReusedIdFixFlow(issue_id.removeprefix(REUSED_ISSUE_PREFIX))
