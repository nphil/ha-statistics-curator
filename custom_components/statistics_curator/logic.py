"""Decision logic for the statistics curator.

Pure functions only: no Home Assistant imports, no I/O. Everything that
mutates the recorder lives in ``__init__``; everything that decides *whether*
to lives here so it can be tested without a running Core.

A removal record looks like::

    {"domain": "sensor", "platform": "mqtt", "unique_id": "abc",
     "device_id": "…" | None, "removed_at": 1700000000.0,
     "id_reused": True}          # only once another identity took the id
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

Identity = tuple[str, str, str]  # (domain, platform, unique_id)
Record = dict[str, Any]
Records = dict[str, Record]

LEFT_ALONE = "left alone"
CLEARED = "cleared"
MIGRATED = "migrated"

Action = Literal["left alone", "cleared", "migrated"]
OPTION_CLEAR_DISABLED = "clear_disabled"
OPTION_MIGRATE_SUCCESSORS = "migrate_successors"
OPTION_CLEAR_DELETED = "clear_deleted"
OPTION_CLEAR_UNWITNESSED = "clear_unwitnessed"
OPTION_DEBOUNCE_SECONDS = "debounce_seconds"
OPTION_DISMISS_SPOOK_ISSUE = "dismiss_spook_issue"

MIN_DEBOUNCE_SECONDS = 0
DEFAULT_DEBOUNCE_SECONDS = 30
MAX_DEBOUNCE_SECONDS = 600


@dataclass(frozen=True, slots=True)
class CuratorOptions:
    """Runtime policy selected in the integration's options flow."""

    clear_disabled: bool = True
    migrate_successors: bool = True
    clear_deleted: bool = True
    clear_unwitnessed: bool = False
    debounce_seconds: int = DEFAULT_DEBOUNCE_SECONDS
    dismiss_spook_issue: bool = True


def options_from_mapping(options: Mapping[str, Any]) -> CuratorOptions:
    """Read persisted options while preserving defaults for older entries."""
    defaults = CuratorOptions()
    debounce_seconds = options.get(OPTION_DEBOUNCE_SECONDS, defaults.debounce_seconds)
    if (
        type(debounce_seconds) is not int
        or not MIN_DEBOUNCE_SECONDS <= debounce_seconds <= MAX_DEBOUNCE_SECONDS
    ):
        debounce_seconds = defaults.debounce_seconds

    def bool_option(name: str, default: bool) -> bool:
        value = options.get(name, default)
        return value if isinstance(value, bool) else default

    return CuratorOptions(
        clear_disabled=bool_option(OPTION_CLEAR_DISABLED, defaults.clear_disabled),
        migrate_successors=bool_option(OPTION_MIGRATE_SUCCESSORS, defaults.migrate_successors),
        clear_deleted=bool_option(OPTION_CLEAR_DELETED, defaults.clear_deleted),
        clear_unwitnessed=bool_option(OPTION_CLEAR_UNWITNESSED, defaults.clear_unwitnessed),
        debounce_seconds=debounce_seconds,
        dismiss_spook_issue=bool_option(
            OPTION_DISMISS_SPOOK_ISSUE, defaults.dismiss_spook_issue
        ),
    )


def identity_of(record: Record) -> Identity:
    return (record["domain"], record["platform"], record["unique_id"])


# --------------------------------------------------------------------------- #
# Registry witnesses
# --------------------------------------------------------------------------- #


def witness_removal(
    records: Records,
    entity_id: str,
    platform: str,
    unique_id: str,
    device_id: str | None,
    now: float,
) -> Literal["recorded", "reuser_left"]:
    """Record that ``entity_id`` was removed.

    If a record for the same id already belongs to a *different* identity,
    the entity leaving now is the one that reused the id. The original
    owner's record is the evidence worth keeping; it is flagged and left
    intact. The reuser's rows are inseparable from it under the same
    statistic_id anyway.
    """
    existing = records.get(entity_id)
    if existing is not None and (existing["platform"], existing["unique_id"]) != (platform, unique_id):
        existing["id_reused"] = True
        return "reuser_left"
    records[entity_id] = {
        "domain": entity_id.split(".", 1)[0],
        "platform": platform,
        "unique_id": unique_id,
        "device_id": device_id,
        "removed_at": now,
    }
    return "recorded"


@dataclass(frozen=True, slots=True)
class Creation:
    """What a registry creation means for the removal memory."""

    kind: Literal["none", "restored", "successor", "reused"]
    old_id: str | None = None


def witness_creation(
    records: Records,
    entity_id: str,
    domain: str,
    platform: str,
    unique_id: str,
    previous_unique_id: str | None,
) -> Creation:
    """Classify a newly created entity against the removal memory.

    * ``restored``: same identity, same id — nothing to move; record dropped.
    * ``successor``: same identity, different id — migration candidate; the
      record is kept until the migration succeeds.
    * ``reused``: a different identity took a recorded id — flagged.

    One creation can be both the successor of one record and the reuser of
    another (its new id was somebody else's old id); the reuse is flagged
    and the successor relationship is what is returned.
    """
    key = (domain, platform, unique_id)
    previous_key = (domain, platform, previous_unique_id) if previous_unique_id else None
    result = Creation("none")
    for old_id, rec in list(records.items()):
        rec_key = identity_of(rec)
        if rec_key == key or (previous_key is not None and rec_key == previous_key):
            if old_id == entity_id:
                del records[old_id]
                return Creation("restored", old_id)
            result = Creation("successor", old_id)
        elif old_id == entity_id:
            rec["id_reused"] = True
            if result.kind == "none":
                result = Creation("reused", old_id)
    return result


def find_successor(records_key: Identity, live: list[tuple[str, Identity, str | None, bool]]) -> str | None:
    """Return the entity_id of an enabled live entity with this identity.

    ``live`` rows are ``(entity_id, identity, previous_unique_id, enabled)``.
    """
    domain, platform, unique_id = records_key
    for entity_id, identity, previous_unique_id, enabled in live:
        if not enabled:
            continue
        if identity == records_key:
            return entity_id
        if previous_unique_id and (identity[0], identity[1], previous_unique_id) == records_key:
            return entity_id
    return None


# --------------------------------------------------------------------------- #
# Settlement decisions
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Orphan:
    """Everything the settlement pass knows about one orphaned statistic."""

    statistic_id: str
    registered: bool  # an entity with this id exists in the registry
    disabled_by: str | None
    record: Record | None
    device_exists: bool  # the recorded device is still in the device registry
    successor_id: str | None


@dataclass(frozen=True, slots=True)
class Decision:
    verb: Literal["clear", "migrate", "skip"]
    action: Action
    detail: str


def decide(orphan: Orphan, options: CuratorOptions) -> Decision:
    """Decide what to do with one orphan. Pure; the caller performs it."""
    sid = orphan.statistic_id
    if orphan.registered:
        if orphan.disabled_by is not None:
            if not options.clear_disabled:
                return Decision(
                    "skip",
                    LEFT_ALONE,
                    f"entity is disabled (`{orphan.disabled_by}`), but the `{OPTION_CLEAR_DISABLED}` option is switched off",
                )
            return Decision(
                "clear",
                CLEARED,
                f"entity is disabled (`{orphan.disabled_by}`); a disabled entity strands "
                "its statistics exactly like a deleted one",
            )
        return Decision(
            "skip",
            LEFT_ALONE,
            "entity exists and is enabled but has no state right now (integration not loaded?) — nothing done",
        )

    rec = orphan.record
    if rec is None:
        if options.clear_unwitnessed:
            return Decision("clear", CLEARED, "no removal record; cleared on operator request")
        return Decision(
            "skip",
            LEFT_ALONE,
            "its removal was not witnessed, so nothing proves the device is gone; clear it from "
            "Settings → Tools → Statistics, or run `statistics_curator.curate` with `clear_unwitnessed: true`",
        )

    identity = f"`{rec['platform']}` / `{rec['unique_id']}`"
    if rec.get("id_reused"):
        return Decision(
            "skip",
            LEFT_ALONE,
            f"the id was reused by another entity after {identity} was removed, so these statistics "
            "mix two devices; clear them by hand if neither is wanted",
        )
    if orphan.successor_id is not None and orphan.successor_id != sid:
        if not options.migrate_successors:
            return Decision(
                "skip",
                LEFT_ALONE,
                f"exact successor `{orphan.successor_id}` exists, but the `{OPTION_MIGRATE_SUCCESSORS}` option is switched off",
            )
        return Decision("migrate", MIGRATED, orphan.successor_id)
    if orphan.device_exists:
        return Decision(
            "skip",
            LEFT_ALONE,
            f"entity ({identity}) was removed but its device still exists; delete the device if it is "
            "really gone, or clear the statistic by hand",
        )
    if not options.clear_deleted:
        return Decision(
            "skip",
            LEFT_ALONE,
            f"entity ({identity}) and its device were deleted, but the `{OPTION_CLEAR_DELETED}` option is switched off",
        )
    return Decision(
        "clear",
        CLEARED,
        f"entity ({identity}) and its device were deleted, and nothing with that identity exists now",
    )


@dataclass(frozen=True, slots=True)
class Series:
    """What the recorder holds for the old and new ids."""

    old_exists: bool
    old_has_sum: bool
    old_unit: str | None
    old_last_start: float | None  # latest row start (hourly or 5-minute)
    new_exists: bool
    new_has_sum: bool | None
    new_unit: str | None
    new_first_start: float | None  # earliest hourly row of the successor


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    method: Literal["rename", "import", "refuse"]
    detail: str


def plan_migration(old_id: str, new_id: str, record: Record, holder_identity: Identity | None, series: Series) -> MigrationPlan:
    """Decide how (or whether) old_id's statistics move under new_id.

    Refuses while another entity holds old_id, or if one ever did: rows are
    bucketed, so a reuser's updates land in the 5-minute/hourly row that was
    open at the removal and no timestamp can prove the series clean. Also
    refuses if anything postdates the witnessed removal, if the two series
    differ in kind, or if merging would break a running total.
    """
    if not series.old_exists:
        return MigrationPlan("refuse", f"no statistics found to migrate to `{new_id}`")
    if holder_identity is not None:
        return MigrationPlan(
            "refuse",
            f"`{old_id}` is now used by another entity (`{holder_identity[1]}` / `{holder_identity[2]}`), "
            f"so its history cannot be proven clean; not moving it to `{new_id}` — decide manually",
        )
    if record.get("id_reused"):
        return MigrationPlan(
            "refuse",
            f"`{old_id}` was reused by another entity after the removal, so its history cannot be "
            f"proven clean; not moving it to `{new_id}` — decide manually",
        )
    if series.old_last_start is not None and series.old_last_start > record["removed_at"]:
        return MigrationPlan(
            "refuse",
            f"statistics were recorded under `{old_id}` after its entity was removed; "
            f"not moving them to `{new_id}` — decide manually",
        )
    if not series.new_exists:
        return MigrationPlan("rename", f"renamed statistics to `{new_id}`")
    if series.old_has_sum != series.new_has_sum or series.old_unit != series.new_unit:
        kind = lambda s, u: f"{u}/{'sum' if s else 'mean'}"  # noqa: E731
        return MigrationPlan(
            "refuse",
            f"`{new_id}` records a different kind of statistic ({kind(series.old_has_sum, series.old_unit)} vs "
            f"{kind(series.new_has_sum, series.new_unit)}); not merging",
        )
    if series.new_first_start is not None:
        if series.old_has_sum:
            return MigrationPlan(
                "refuse",
                f"`{new_id}` already has a running total; merging `{old_id}` into it would break the sum — decide manually",
            )
        if series.old_last_start is not None and series.old_last_start >= series.new_first_start:
            return MigrationPlan("refuse", f"`{old_id}` and `{new_id}` overlap in time; not merging")
    return MigrationPlan("import", f"merged rows into `{new_id}`, which already existed")


def prunable(records: Records, has_statistics: set[str]) -> list[str]:
    """Records with no statistics left under their id.

    Such a record can never lead to anything: there is nothing to migrate to
    a successor and nothing to warn about, and statistics cannot reappear
    under a removed entity's id except through a reuser, whose own removal
    would create a fresh record. Most removals (entities that never had
    statistics) are forgotten on the next pass this way.
    """
    return [entity_id for entity_id in records if entity_id not in has_statistics]


def summary_footer(
    results: list[tuple[str, Action, str]],
    remaining: list[str] | None,
    dismiss_spook_issue: bool = True,
) -> list[str]:
    """Closing lines for the notification.

    ``remaining`` is what validation still reports; ``None`` means no
    settlement pass ran. Refusals are called out separately because a reused
    id hides from validation (the reuser has a live state) while its history
    is still misattributed. A clean pass only claims dismissal when that
    policy is enabled.
    """
    lines: list[str] = []
    if remaining:
        lines += ["Still orphaned (Spook's repair stays open):"]
        lines += [f"- `{s}`" for s in remaining]
    elif any(action == LEFT_ALONE for _, action, _ in results):
        lines += [
            "Spook reports nothing further, but the items marked *left alone* still need your decision; "
            "the ones with a reused id stay listed under Settings → Repairs until settled."
        ]
    elif remaining is not None:
        if dismiss_spook_issue:
            lines += ["No orphaned statistics remain; Spook's repair was dismissed."]
        else:
            lines += [
                "No orphaned statistics remain; Spook's repair was left open because the `dismiss_spook_issue` option is switched off."
            ]
    return lines
