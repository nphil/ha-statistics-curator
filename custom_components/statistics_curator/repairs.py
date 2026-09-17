"""Fix flows for this integration's own repairs.

Both repairs are settled by clearing a series, which is the operator's call:
a mixed one can no longer be attributed, and an unwitnessed one has no
evidence its device is gone. Offering that as the repair's Fix button keeps
the operator on one screen and lets the curator retire the issue in the same
step, instead of waiting for the periodic prune to notice a clear done from
the Statistics page.
"""

from __future__ import annotations

from typing import Any, cast

import voluptuous as vol

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from . import (
    DOMAIN,
    REUSED_ISSUE_PREFIX,
    UNWITNESSED_ISSUE_PREFIX,
    Curator,
    StatisticsCuratorConfigEntry,
)


def _loaded_curator(hass: HomeAssistant) -> Curator | None:
    """Return the active entry's curator, if its repair can be safely handled."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED:
            return cast(StatisticsCuratorConfigEntry, entry).runtime_data
    return None


class _ClearFixFlow(RepairsFlow):
    """Confirm, then clear one statistic id."""

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
            await self._async_clear(curator)
            return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={"statistic_id": self.statistic_id},
        )

    async def _async_clear(self, curator: Curator) -> None:
        raise NotImplementedError


class ReusedIdFixFlow(_ClearFixFlow):
    """Clear a series two entities wrote to."""

    async def _async_clear(self, curator: Curator) -> None:
        await curator.async_clear_and_forget(self.statistic_id)


class UnwitnessedFixFlow(_ClearFixFlow):
    """Clear a series whose entity's removal was never observed."""

    async def _async_clear(self, curator: Curator) -> None:
        await curator.async_clear_unwitnessed(self.statistic_id)


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    if issue_id.startswith(UNWITNESSED_ISSUE_PREFIX):
        return UnwitnessedFixFlow(issue_id.removeprefix(UNWITNESSED_ISSUE_PREFIX))
    return ReusedIdFixFlow(issue_id.removeprefix(REUSED_ISSUE_PREFIX))
