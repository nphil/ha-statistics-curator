"""Config and options flows for Statistics Curator."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers import selector

from . import DOMAIN
from . import logic

ENTRY_TITLE = "Statistics Curator"


# A NumberSelector always hands back a float, and voluptuous validators wrapped
# around a selector cannot be serialized for the frontend, so the value is
# narrowed to whole seconds when the form is saved instead.


def _options_schema(options: logic.CuratorOptions) -> vol.Schema:
    """Build controls from the entry's effective policy."""
    return vol.Schema(
        {
            vol.Required(
                logic.OPTION_CLEAR_DISABLED, default=options.clear_disabled
            ): selector.BooleanSelector(),
            vol.Required(
                logic.OPTION_MIGRATE_SUCCESSORS, default=options.migrate_successors
            ): selector.BooleanSelector(),
            vol.Required(
                logic.OPTION_CLEAR_DELETED, default=options.clear_deleted
            ): selector.BooleanSelector(),
            vol.Required(
                logic.OPTION_CLEAR_UNWITNESSED, default=options.clear_unwitnessed
            ): selector.BooleanSelector(),
            vol.Required(
                logic.OPTION_DEBOUNCE_SECONDS, default=options.debounce_seconds
            ): selector.NumberSelector(
                {
                    "min": logic.MIN_DEBOUNCE_SECONDS,
                    "max": logic.MAX_DEBOUNCE_SECONDS,
                    "step": 1,
                    "mode": selector.NumberSelectorMode.BOX,
                    "unit_of_measurement": "s",
                }
            ),
            vol.Required(
                logic.OPTION_DISMISS_SPOOK_ISSUE,
                default=options.dismiss_spook_issue,
            ): selector.BooleanSelector(),
        }
    )


class StatisticsCuratorConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle Statistics Curator's single config entry."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Confirm setup without collecting configuration data."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        self._async_abort_entries_match()

        if user_input is not None:
            return self.async_create_entry(title=ENTRY_TITLE, data={})

        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the policy editor for the entry."""
        return StatisticsCuratorOptionsFlow()


class StatisticsCuratorOptionsFlow(config_entries.OptionsFlow):
    """Edit the policy applied to future settlement passes."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Show or save the runtime policy."""
        if user_input is not None:
            saved = dict(user_input)
            saved[logic.OPTION_DEBOUNCE_SECONDS] = int(
                float(saved[logic.OPTION_DEBOUNCE_SECONDS])
            )
            return self.async_create_entry(title="", data=saved)

        options = logic.options_from_mapping(self.config_entry.options)
        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(options),
        )
