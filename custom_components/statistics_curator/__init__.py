"""Statistics Curator.

Settles long-term statistics that no longer have a live entity behind them,
the condition Spook reports as its ``orphaned_statistics`` repair.

Home Assistant already migrates statistics on an entity rename (recorder's
entity-registry hook) and restores a deleted entity's id when the same
``(domain, platform, unique_id)`` is re-created, so the orphans that remain
are genuinely one of:

* the entity was deleted, its device is gone too (or it never had one), and
  nothing with its identity came back -> clear;
* the entity exists but is disabled -> clear (a disabled entity strands its
  statistics exactly like a deleted one; house rule 1);
* the entity was deleted and later came back under a different entity_id
  (re-added after the registry forgot it) -> migrate.

Both the deletion and the re-creation cases are only recognisable if the
removal was witnessed, because the registry forgets the identity and device
the moment the entity is gone or re-created. This integration therefore
records every removal as it happens (identity + device) and acts only on that
evidence. It never guesses from names or units, and it never clears history
it did not see being deleted: an orphan with no removal record is reported and
left in place (the ``curate`` service accepts ``clear_unwitnessed: true`` for
the operator to settle those deliberately).

Anything else it cannot settle safely is reported in the notification and
left for the operator, and Spook's repair stays open until nothing is left:
an enabled entity whose integration is not loaded; an entity removed while
its device still exists; a sum-type series that would collide with data the
successor already has; and any id that a *different* identity took after the
removal — statistics rows are bucketed, so the reuser's data can share the
bucket that was open at the removal, and such a series is mixed for good
(neither migrated nor cleared automatically). Because a reuser's live state
hides that last case from Spook, it gets its own repair issue here, which
stays until the operator clears the statistics.

All decisions live in ``logic.py`` (pure, unit-tested); this module is the
plumbing that gathers facts and performs the chosen recorder operations.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
import logging
import time
from typing import Any, TypeAlias

import voluptuous as vol

from homeassistant.components import persistent_notification
from homeassistant.components.recorder.db_schema import Statistics, StatisticsShortTerm
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType
from homeassistant.components.recorder.statistics import (
    get_metadata,
    statistics_during_period,
    validate_statistics,
)
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util.dt import utcnow

from . import logic
from .logic import LEFT_ALONE, MIGRATED, Action, Orphan, Record, Records, Series

_LOGGER = logging.getLogger(__name__)

DOMAIN = "statistics_curator"
SERVICE_CURATE = "curate"

SPOOK_DOMAIN = "spook"
# Spook builds ids as f"{repair}_{issue_id}" and this repair passes its own name twice.
SPOOK_ISSUE_ID = "orphaned_statistics_orphaned_statistics"
ORPHAN_ISSUE_TYPE = "no_state"

REUSED_ISSUE_PREFIX = "id_reused_"

NOTIFICATION_ID = "statistics_curator"
NOTIFICATION_TITLE = "Statistics curator"

STORAGE_KEY = f"{DOMAIN}.removed_entities"
STORAGE_VERSION = 1
# A removal record older than this is unlikely to ever be matched; the
# registry's own deleted-entity memory is also bounded.
REMOVAL_KEEP_SECONDS = 180 * 24 * 3600

# A series cleared from the Statistics page fires no event; this is how long
# a settled reused-id repair can outlive the clear before the prune notices.
PRUNE_INTERVAL = timedelta(hours=1)

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


Result = tuple[str, Action, str]


class Curator:
    """Owns the removal memory and the settlement pass."""

    def __init__(self, hass: HomeAssistant, options: logic.CuratorOptions) -> None:
        self.hass = hass
        self.options = options
        self._store: Store[Records] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.removed: Records = {}
        # Live entity_id -> device_id, kept because a removal event arrives
        # after the registry entry (and its device_id) is already gone.
        self._device_of: dict[str, str | None] = {}
        self._lock = asyncio.Lock()
        self._pending_cancel: Callable[[], None] | None = None

    async def async_load(self) -> None:
        data = await self._store.async_load()
        cutoff = time.time() - REMOVAL_KEEP_SECONDS
        self.removed = {
            entity_id: rec
            for entity_id, rec in (data or {}).items()
            if rec.get("removed_at", 0) >= cutoff
        }
        self._device_of = {
            entry.entity_id: entry.device_id
            for entry in er.async_get(self.hass).entities.values()
        }
        for entity_id, rec in self.removed.items():
            if rec.get("id_reused"):
                self.hass.async_create_task(
                    self._async_raise_reused_issue(entity_id, rec)
                )
        self._retire_unbacked_issues()

    @callback
    def _retire_unbacked_issues(self) -> None:
        """Drop reused-id repairs whose evidence is gone.

        These repairs are persistent, so one raised for a record that has
        since been settled or forgotten would otherwise sit in Settings →
        Repairs for good.
        """
        flagged = {
            REUSED_ISSUE_PREFIX + entity_id
            for entity_id, rec in self.removed.items()
            if rec.get("id_reused")
        }
        for (domain, issue_id) in list(ir.async_get(self.hass).issues):
            if domain == DOMAIN and issue_id.startswith(REUSED_ISSUE_PREFIX) and issue_id not in flagged:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                _LOGGER.debug("Retired %s: no removal record backs it any more", issue_id)

    @callback
    def _save(self) -> None:
        self._store.async_delay_save(lambda: self.removed, 5)

    # ------------------------------------------------------------------ #
    # Registry witnesses
    # ------------------------------------------------------------------ #

    @callback
    def handle_registry_event(self, event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        action = event.data["action"]
        entity_id = event.data["entity_id"]
        registry = er.async_get(self.hass)

        if action == "remove":
            device_id = self._device_of.pop(entity_id, None)
            # The live entry is gone, but the registry keeps a deleted copy
            # keyed by identity; find it by entity_id.
            deleted = next((d for d in registry.deleted_entities.values() if d.entity_id == entity_id), None)
            if deleted is None:
                _LOGGER.debug("Removal of %s left no deleted-entity record", entity_id)
                return
            outcome = logic.witness_removal(
                self.removed, entity_id, deleted.platform, deleted.unique_id, device_id, time.time()
            )
            self._save()
            if outcome == "reuser_left":
                self.hass.async_create_task(
                    self._async_raise_reused_issue(entity_id, self.removed[entity_id])
                )
                _LOGGER.debug("Reuser of %s removed; keeping the original record", entity_id)
            else:
                _LOGGER.debug(
                    "Witnessed removal of %s (%s/%s, device %s)",
                    entity_id, deleted.platform, deleted.unique_id, device_id,
                )
            return

        if action == "update":
            if (old := event.data.get("old_entity_id")) is not None:
                self._device_of.pop(old, None)
            if (entry := registry.async_get(entity_id)) is not None:
                self._device_of[entity_id] = entry.device_id
            return

        if action == "create":
            entry = registry.async_get(entity_id)
            if entry is None:
                return
            self._device_of[entity_id] = entry.device_id
            outcome = logic.witness_creation(
                self.removed, entity_id, entry.domain, entry.platform, entry.unique_id, entry.previous_unique_id
            )
            if outcome.kind == "none":
                return
            self._save()
            if outcome.kind == "restored":
                _LOGGER.debug("%s came back under its own id", entity_id)
                return
            if entity_id in self.removed and self.removed[entity_id].get("id_reused"):
                self.hass.async_create_task(
                    self._async_raise_reused_issue(entity_id, self.removed[entity_id])
                )
                _LOGGER.info(
                    "%s was reused by %s/%s; its old statistics are now unmigratable",
                    entity_id, entry.platform, entry.unique_id,
                )
            if outcome.kind == "successor":
                if not self.options.migrate_successors:
                    _LOGGER.debug(
                        "%s is the successor of %s; migration is disabled by options",
                        entity_id,
                        outcome.old_id,
                    )
                else:
                    _LOGGER.debug("%s is the successor of %s; migrating", entity_id, outcome.old_id)
                    self.hass.async_create_task(
                        self._async_migrate_on_recreate(outcome.old_id, entity_id)
                    )

    async def _async_migrate_on_recreate(self, old_id: str, new_id: str) -> None:
        """Mirror the recorder's rename hook for a delete+recreate."""
        instance = get_instance(self.hass)
        metas = await instance.async_add_executor_job(
            partial(get_metadata, self.hass, statistic_ids={old_id})
        )
        if old_id not in metas:
            # Nothing recorded under the old id; the record has served its purpose.
            self.removed.pop(old_id, None)
            self._save()
            return
        async with self._lock:
            result = await self._async_migrate(old_id, new_id, self.removed[old_id])
            if result[1] == MIGRATED:
                self.removed.pop(old_id, None)
                self._save()
            _LOGGER.info("Recreated entity %s: %s", new_id, result[2])
            remaining = await self._async_orphans()
            if not remaining:
                await self._async_resolve_spook_issue()
            self._notify([result], remaining)

    # ------------------------------------------------------------------ #
    # Settlement pass
    # ------------------------------------------------------------------ #

    @callback
    def cancel_pending(self) -> None:
        """Cancel a delayed curation before its entry goes away."""
        if self._pending_cancel is not None:
            self._pending_cancel()
            self._pending_cancel = None

    @callback
    def schedule(self, _: Any = None) -> None:
        self.cancel_pending()

        @callback
        def _fire(_now: datetime) -> None:
            self._pending_cancel = None
            self.hass.async_create_task(self.async_curate())

        self._pending_cancel = async_call_later(
            self.hass, self.options.debounce_seconds, _fire
        )

    async def async_curate(self, clear_unwitnessed: bool | None = None) -> list[Result]:
        """Settle every orphan Spook would report. Returns (id, action, detail)."""
        options = self.options
        if clear_unwitnessed is not None:
            options = replace(options, clear_unwitnessed=clear_unwitnessed)
        async with self._lock:
            return await self._async_curate(options)

    async def _async_curate(self, options: logic.CuratorOptions) -> list[Result]:
        hass = self.hass
        registry = er.async_get(hass)
        devices = dr.async_get(hass)

        await self._async_prune_records()
        orphans = await self._async_orphans()
        if not orphans:
            await self._async_resolve_spook_issue()
            return []

        live = [
            (e.entity_id, (e.domain, e.platform, e.unique_id), e.previous_unique_id, e.disabled_by is None)
            for e in registry.entities.values()
        ]

        results: list[Result] = []
        for statistic_id in orphans:
            entry = registry.async_get(statistic_id)
            rec = self.removed.get(statistic_id)
            device_id = rec.get("device_id") if rec else None
            orphan = Orphan(
                statistic_id=statistic_id,
                registered=entry is not None,
                disabled_by=entry.disabled_by if entry else None,
                record=rec,
                device_exists=bool(device_id) and devices.async_get(device_id) is not None,
                successor_id=logic.find_successor(logic.identity_of(rec), live) if rec else None,
            )
            decision = logic.decide(orphan, options)

            if decision.verb == "migrate":
                result = await self._async_migrate(statistic_id, decision.detail, rec)
            elif decision.verb == "clear":
                await self._async_clear(statistic_id)
                result = (statistic_id, decision.action, decision.detail)
            else:
                result = (statistic_id, decision.action, decision.detail)

            if result[1] != LEFT_ALONE and rec is not None:
                self.removed.pop(statistic_id, None)
                self._save()
            results.append(result)

        for statistic_id, action, detail in results:
            _LOGGER.info("%s %s: %s", action, statistic_id, detail)
        remaining = await self._async_orphans()
        if not remaining:
            await self._async_resolve_spook_issue()
        self._notify(results, remaining)
        return results

    async def async_clear_and_forget(self, statistic_id: str) -> None:
        """The reused-id repair's Fix: clear the mixed series and retire the record."""
        async with self._lock:
            await self._async_clear(statistic_id)
            self.removed.pop(statistic_id, None)
            self._save()
            ir.async_delete_issue(self.hass, DOMAIN, REUSED_ISSUE_PREFIX + statistic_id)
            _LOGGER.info("cleared %s: mixed series cleared on operator request via Repairs", statistic_id)
            self._notify([(statistic_id, "cleared", "mixed series cleared on operator request via Repairs")])

    async def async_prune(self) -> None:
        async with self._lock:
            await self._async_prune_records()

    async def _async_prune_records(self) -> None:
        """Forget removals with no statistics left; see ``logic.prunable``.

        Also retires the reused-id repair for them: once the mixed series is
        cleared by hand there is nothing left to warn about.
        """
        if not self.removed:
            return
        instance = get_instance(self.hass)
        metas = await instance.async_add_executor_job(
            partial(get_metadata, self.hass, statistic_ids=set(self.removed))
        )
        stale = logic.prunable(self.removed, set(metas))
        for entity_id in stale:
            del self.removed[entity_id]
            ir.async_delete_issue(self.hass, DOMAIN, REUSED_ISSUE_PREFIX + entity_id)
        if stale:
            self._save()
            _LOGGER.debug("Forgot %d removal record(s) with nothing left to act on", len(stale))

    async def _async_orphans(self) -> list[str]:
        instance = get_instance(self.hass)
        validation = await instance.async_add_executor_job(validate_statistics, self.hass)
        return sorted(
            statistic_id
            for statistic_id, issues in validation.items()
            if any(issue.type == ORPHAN_ISSUE_TYPE for issue in issues)
        )

    # ------------------------------------------------------------------ #
    # Recorder operations (all through the recorder's own queue/executor)
    # ------------------------------------------------------------------ #

    def _done_signal(self) -> tuple[asyncio.Future[None], Any]:
        """A future plus the recorder-thread callback that resolves it.

        ``async_block_till_done`` only waits for the next commit, which can
        land before a queued task runs; ``on_done`` fires when the task has.
        """
        loop = self.hass.loop
        done: asyncio.Future[None] = loop.create_future()
        return done, lambda: loop.call_soon_threadsafe(done.set_result, None)

    async def _async_clear(self, statistic_id: str) -> None:
        done, on_done = self._done_signal()
        get_instance(self.hass).async_clear_statistics([statistic_id], on_done=on_done)
        await done

    async def _async_migrate(self, old_id: str, new_id: str, rec: Record) -> Result:
        """Move old_id's statistics under new_id as ``logic.plan_migration`` decides."""
        hass = self.hass
        instance = get_instance(hass)
        metas = await instance.async_add_executor_job(
            partial(get_metadata, hass, statistic_ids={old_id, new_id})
        )
        old_meta = metas[old_id][1] if old_id in metas else None
        new_meta = metas[new_id][1] if new_id in metas else None
        holder = er.async_get(hass).async_get(old_id)

        types: set[str] = set()
        old_hourly: list[dict[str, Any]] = []
        old_short: list[dict[str, Any]] = []
        new_hourly: list[dict[str, Any]] = []
        if old_meta is not None:
            if old_meta.get("mean_type", StatisticMeanType.NONE) != StatisticMeanType.NONE:
                types |= {"mean", "min", "max"}
            if old_meta["has_sum"]:
                types |= {"state", "sum", "last_reset"}
            old_hourly, old_short, new_hourly = await instance.async_add_executor_job(
                partial(self._fetch_rows, old_id, new_id, types)
            )

        series = Series(
            old_exists=old_meta is not None,
            old_has_sum=bool(old_meta and old_meta["has_sum"]),
            old_unit=old_meta["unit_of_measurement"] if old_meta else None,
            old_last_start=max((r["start"] for r in old_hourly + old_short), default=None),
            new_exists=new_meta is not None,
            new_has_sum=new_meta["has_sum"] if new_meta else None,
            new_unit=new_meta["unit_of_measurement"] if new_meta else None,
            new_first_start=new_hourly[0]["start"] if new_hourly else None,
        )
        plan = logic.plan_migration(
            old_id, new_id, rec,
            (holder.domain, holder.platform, holder.unique_id) if holder else None,
            series,
        )

        if plan.method == "refuse":
            return (old_id, LEFT_ALONE, plan.detail)

        if plan.method == "rename":
            done, on_done = self._done_signal()
            instance.async_update_statistics_metadata(old_id, new_statistic_id=new_id, on_done=on_done)
            await done
            after = await instance.async_add_executor_job(
                partial(get_metadata, hass, statistic_ids={old_id, new_id})
            )
            if new_id in after and old_id not in after:
                return (old_id, MIGRATED, plan.detail)
            return (old_id, LEFT_ALONE, f"recorder refused to rename to `{new_id}`")

        assert new_meta is not None
        if old_hourly:
            instance.async_import_statistics(new_meta, _to_statistic_data(old_hourly, types), Statistics)
        if old_short:
            instance.async_import_statistics(new_meta, _to_statistic_data(old_short, types), StatisticsShortTerm)
        await instance.async_block_till_done()
        await self._async_clear(old_id)
        return (old_id, MIGRATED, f"merged {len(old_hourly)} hourly rows into `{new_id}`, which already existed")

    def _fetch_rows(self, old_id: str, new_id: str, types: set[str]):
        hass = self.hass
        old_hourly = statistics_during_period(hass, EPOCH, None, {old_id}, "hour", None, types).get(old_id, [])
        old_short = statistics_during_period(hass, EPOCH, None, {old_id}, "5minute", None, types).get(old_id, [])
        new_hourly = statistics_during_period(hass, EPOCH, None, {new_id}, "hour", None, {"mean", "sum"}).get(new_id, [])
        return old_hourly, old_short, new_hourly

    # ------------------------------------------------------------------ #
    # Reporting
    # ------------------------------------------------------------------ #

    async def _async_raise_reused_issue(self, entity_id: str, rec: Record) -> None:
        """Warn about a reused id, but only when history is actually at stake.

        A reused id hides from Spook, because the newcomer has a live state,
        so it needs a repair of its own. Most reuse is harmless though: entity
        ids are recycled all the time by domains that keep no statistics at
        all, and warning about those would be pure noise. So the repair is
        raised only while the recorder still holds a series under that id, and
        the record is dropped when it does not.
        """
        instance = get_instance(self.hass)
        metas = await instance.async_add_executor_job(
            partial(get_metadata, self.hass, statistic_ids={entity_id})
        )
        if entity_id not in metas:
            self.removed.pop(entity_id, None)
            self._save()
            # The repair is persistent, so one raised before the series was
            # cleared would otherwise outlive it across restarts.
            ir.async_delete_issue(self.hass, DOMAIN, REUSED_ISSUE_PREFIX + entity_id)
            _LOGGER.debug("%s was reused but holds no statistics; nothing to warn about", entity_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            REUSED_ISSUE_PREFIX + entity_id,
            is_fixable=True,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="id_reused",
            translation_placeholders={
                "statistic_id": entity_id,
                "platform": rec["platform"],
                "unique_id": rec["unique_id"],
            },
        )

    async def _async_resolve_spook_issue(self) -> None:
        """Retire Spook's repair now that validation reports no orphan.

        Spook's repairs are cached snapshots that only clear on its own
        re-check, so the honest way to dismiss one is to make Spook look
        again: reload its config entry and let its inspection remove the
        issue. Direct deletion is the fallback for when Spook is not loaded
        or has not caught up, and is only reached after our own
        ``validate_statistics`` pass found nothing.
        """
        if not self.options.dismiss_spook_issue:
            return
        hass = self.hass
        issues = ir.async_get(hass)
        if issues.async_get_issue(SPOOK_DOMAIN, SPOOK_ISSUE_ID) is None:
            return
        for entry in hass.config_entries.async_entries(SPOOK_DOMAIN):
            if entry.state is not ConfigEntryState.LOADED:
                continue
            await hass.config_entries.async_reload(entry.entry_id)
            for _ in range(20):
                if issues.async_get_issue(SPOOK_DOMAIN, SPOOK_ISSUE_ID) is None:
                    _LOGGER.debug("Spook's re-inspection cleared its repair")
                    return
                await asyncio.sleep(1)
            break
        _LOGGER.info("Spook did not retire its repair on re-check; dismissing it directly")
        ir.async_delete_issue(hass, SPOOK_DOMAIN, SPOOK_ISSUE_ID)

    @callback
    def _notify(self, results: list[Result], remaining: list[str] | None = None) -> None:
        if not results and not remaining:
            return
        lines = [f"Run at {utcnow().strftime('%Y-%m-%d %H:%M UTC')}.", ""]
        for statistic_id, action, detail in results:
            lines.append(f"- **{action}** `{statistic_id}` — {detail}")
        footer = logic.summary_footer(
            results, remaining, self.options.dismiss_spook_issue
        )
        if footer:
            lines += ["", *footer]
        persistent_notification.async_create(
            self.hass,
            "\n".join(lines),
            title=NOTIFICATION_TITLE,
            notification_id=NOTIFICATION_ID,
        )

StatisticsCuratorConfigEntry: TypeAlias = ConfigEntry[Curator]

def _to_statistic_data(rows: list[dict[str, Any]], types: set[str]) -> list[StatisticData]:
    out: list[StatisticData] = []
    for row in rows:
        item: dict[str, Any] = {"start": datetime.fromtimestamp(row["start"], tz=UTC)}
        for key in ("mean", "min", "max", "state", "sum"):
            if key in types and row.get(key) is not None:
                item[key] = row[key]
        if "last_reset" in types:
            lr = row.get("last_reset")
            item["last_reset"] = datetime.fromtimestamp(lr, tz=UTC) if lr is not None else None
        out.append(item)  # type: ignore[arg-type]
    return out


async def async_setup_entry(
    hass: HomeAssistant, entry: StatisticsCuratorConfigEntry
) -> bool:
    """Set up Statistics Curator from its sole config entry."""
    curator = Curator(hass, logic.options_from_mapping(entry.options))
    await curator.async_load()
    entry.runtime_data = curator

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    entry.async_on_unload(curator.cancel_pending)
    entry.async_on_unload(
        hass.bus.async_listen(
            er.EVENT_ENTITY_REGISTRY_UPDATED, curator.handle_registry_event
        )
    )

    @callback
    def _on_issue(event: Event[ir.EventIssueRegistryUpdatedData]) -> None:
        if (
            event.data["domain"] == SPOOK_DOMAIN
            and event.data["issue_id"] == SPOOK_ISSUE_ID
            and event.data["action"] in ("create", "update")
        ):
            curator.schedule()

    entry.async_on_unload(
        hass.bus.async_listen(ir.EVENT_REPAIRS_ISSUE_REGISTRY_UPDATED, _on_issue)
    )

    @callback
    def _on_device_removed(event: Event[dr.EventDeviceRegistryUpdatedData]) -> None:
        # A device's removal is the evidence that can settle a witnessed orphan.
        if event.data["action"] == "remove" and ir.async_get(hass).async_get_issue(
            SPOOK_DOMAIN, SPOOK_ISSUE_ID
        ):
            curator.schedule()

    entry.async_on_unload(
        hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, _on_device_removed)
    )

    @callback
    def _schedule_if_spook_issue(_hass: HomeAssistant) -> None:
        # Spook may have raised its repair before this entry was loaded.
        # async_at_started fires immediately when Core is already running and
        # returns an unsubscribe that stays valid once the event has passed,
        # which a bare async_listen_once does not.
        if ir.async_get(hass).async_get_issue(SPOOK_DOMAIN, SPOOK_ISSUE_ID):
            curator.schedule()

    entry.async_on_unload(async_at_started(hass, _schedule_if_spook_issue))

    @callback
    def _prune(_: datetime) -> None:
        hass.async_create_task(curator.async_prune())

    entry.async_on_unload(async_track_time_interval(hass, _prune, PRUNE_INTERVAL))

    async def _handle_curate(call: ServiceCall) -> None:
        clear_unwitnessed: bool | None = call.data.get(
            logic.OPTION_CLEAR_UNWITNESSED
        )
        await curator.async_curate(clear_unwitnessed=clear_unwitnessed)

    hass.services.async_register(
        DOMAIN,
        SERVICE_CURATE,
        _handle_curate,
        schema=vol.Schema({vol.Optional(logic.OPTION_CLEAR_UNWITNESSED): cv.boolean}),
    )

    @callback
    def _remove_curate_service() -> None:
        hass.services.async_remove(DOMAIN, SERVICE_CURATE)

    entry.async_on_unload(_remove_curate_service)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: StatisticsCuratorConfigEntry
) -> bool:
    """Unload Statistics Curator."""
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: StatisticsCuratorConfigEntry
) -> None:
    """Reload the entry after its options are saved."""
    await hass.config_entries.async_reload(entry.entry_id)
