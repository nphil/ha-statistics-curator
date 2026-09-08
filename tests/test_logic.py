"""Behavioural tests for the curator's decisions.

These cover the paths that cannot be reproduced against a live Core on
demand (an exact successor under a new id) and the event orderings that
produced wrong outcomes during live testing (id reuse, reuser leaving).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "statistics_curator"
    ),
)

from logic import (  # noqa: E402
    CLEARED,
    LEFT_ALONE,
    MIGRATED,
    CuratorOptions,
    Orphan,
    Series,
    decide,
    find_successor,
    plan_migration,
    prunable,
    summary_footer,
    witness_creation,
    witness_removal,
)

OLD = "sensor.plant_room_probe_temperature"
NEW = "sensor.plant_room_soil_probe_temperature"
T0 = 1_700_000_000.0
DEFAULT_OPTIONS = CuratorOptions()


def removed(**over):
    rec = {"domain": "sensor", "platform": "mqtt", "unique_id": "probe-1", "device_id": "dev1", "removed_at": T0}
    rec.update(over)
    return rec


def series(**over):
    base = dict(
        old_exists=True, old_has_sum=False, old_unit="°C", old_last_start=T0 - 3600,
        new_exists=False, new_has_sum=None, new_unit=None, new_first_start=None,
    )
    base.update(over)
    return Series(**base)


# --------------------------------------------------------------------------- #
# Registry witnesses
# --------------------------------------------------------------------------- #


def test_same_identity_same_id_is_a_restore_not_a_migration():
    records = {OLD: removed()}
    outcome = witness_creation(records, OLD, "sensor", "mqtt", "probe-1", None)
    assert outcome.kind == "restored"
    assert records == {}


def test_same_identity_new_id_is_a_successor_and_keeps_the_record():
    records = {OLD: removed()}
    outcome = witness_creation(records, NEW, "sensor", "mqtt", "probe-1", None)
    assert outcome.kind == "successor" and outcome.old_id == OLD
    assert OLD in records, "record must survive until the migration succeeds"


def test_previous_unique_id_counts_as_the_same_identity():
    records = {OLD: removed(unique_id="legacy-uid")}
    outcome = witness_creation(records, NEW, "sensor", "mqtt", "new-uid", "legacy-uid")
    assert outcome.kind == "successor" and outcome.old_id == OLD


def test_other_platform_with_same_unique_id_is_not_a_successor():
    records = {OLD: removed()}
    assert witness_creation(records, NEW, "sensor", "zha", "probe-1", None).kind == "none"


def test_a_different_identity_taking_the_old_id_flags_reuse_and_keeps_the_record():
    records = {OLD: removed()}
    outcome = witness_creation(records, OLD, "sensor", "mqtt", "squatter", None)
    assert outcome.kind == "reused"
    assert records[OLD]["unique_id"] == "probe-1"
    assert records[OLD]["id_reused"] is True


def test_reuser_leaving_does_not_overwrite_the_original_record():
    records = {OLD: removed()}
    witness_creation(records, OLD, "sensor", "mqtt", "squatter", None)
    assert witness_removal(records, OLD, "mqtt", "squatter", "dev-squat", T0 + 100) == "reuser_left"
    assert records[OLD]["unique_id"] == "probe-1"
    assert records[OLD]["removed_at"] == T0
    assert records[OLD]["id_reused"] is True


def test_reuser_leaving_flags_reuse_even_if_creation_was_missed():
    # e.g. the reuser was created while Core was down and we never saw it
    records = {OLD: removed()}
    witness_removal(records, OLD, "mqtt", "squatter", None, T0 + 100)
    assert records[OLD]["id_reused"] is True


def test_same_identity_removed_again_refreshes_the_record():
    records = {OLD: removed()}
    assert witness_removal(records, OLD, "mqtt", "probe-1", "dev1", T0 + 500) == "recorded"
    assert records[OLD]["removed_at"] == T0 + 500
    assert "id_reused" not in records[OLD]


def test_one_creation_can_be_successor_of_one_record_and_reuser_of_another():
    other = "sensor.other_thing"
    records = {OLD: removed(), other: removed(unique_id="other-uid", device_id="dev2")}
    # New entity: identity probe-1 (successor of OLD), but it got the id `other`
    outcome = witness_creation(records, other, "sensor", "mqtt", "probe-1", None)
    assert outcome == outcome.__class__("successor", OLD)
    assert records[other]["id_reused"] is True
    assert OLD in records


def test_find_successor_ignores_disabled_entities():
    live = [(NEW, ("sensor", "mqtt", "probe-1"), None, False)]
    assert find_successor(("sensor", "mqtt", "probe-1"), live) is None
    live = [(NEW, ("sensor", "mqtt", "probe-1"), None, True)]
    assert find_successor(("sensor", "mqtt", "probe-1"), live) == NEW


# --------------------------------------------------------------------------- #
# Settlement decisions
# --------------------------------------------------------------------------- #


def orphan(**over):
    base = dict(statistic_id=OLD, registered=False, disabled_by=None, record=None, device_exists=False, successor_id=None)
    base.update(over)
    return Orphan(**base)


def test_disabled_entity_is_cleared_per_house_rule():
    d = decide(orphan(registered=True, disabled_by="user"), DEFAULT_OPTIONS)
    assert (d.verb, d.action) == ("clear", CLEARED)


def test_disabled_entity_is_left_alone_when_clearing_is_disabled():
    d = decide(
        orphan(registered=True, disabled_by="user"),
        CuratorOptions(clear_disabled=False),
    )
    assert (d.verb, d.action) == ("skip", LEFT_ALONE)
    assert "clear_disabled" in d.detail and "switched off" in d.detail


def test_enabled_entity_without_state_is_left_alone():
    d = decide(orphan(registered=True), DEFAULT_OPTIONS)
    assert d.verb == "skip"


def test_unwitnessed_orphan_is_left_alone_unless_operator_asks():
    assert decide(orphan(), DEFAULT_OPTIONS).verb == "skip"
    assert decide(orphan(), CuratorOptions(clear_unwitnessed=True)).verb == "clear"


def test_default_policy_leaves_an_unwitnessed_orphan_alone():
    d = decide(orphan(), DEFAULT_OPTIONS)
    assert (d.verb, d.action) == ("skip", LEFT_ALONE)
    assert "not witnessed" in d.detail


def test_witnessed_removal_with_device_gone_is_cleared():
    d = decide(orphan(record=removed(), device_exists=False), DEFAULT_OPTIONS)
    assert d.verb == "clear"


def test_witnessed_device_gone_is_left_alone_when_clearing_is_disabled():
    d = decide(
        orphan(record=removed(), device_exists=False),
        CuratorOptions(clear_deleted=False),
    )
    assert (d.verb, d.action) == ("skip", LEFT_ALONE)
    assert "clear_deleted" in d.detail and "switched off" in d.detail


def test_witnessed_removal_with_device_present_is_left_alone():
    d = decide(orphan(record=removed(), device_exists=True), DEFAULT_OPTIONS)
    assert d.verb == "skip" and "device" in d.detail


def test_witnessed_removal_with_successor_is_migrated():
    d = decide(orphan(record=removed(), successor_id=NEW), DEFAULT_OPTIONS)
    assert (d.verb, d.action, d.detail) == ("migrate", MIGRATED, NEW)


def test_exact_successor_is_left_alone_when_migration_is_disabled():
    d = decide(
        orphan(record=removed(), successor_id=NEW),
        CuratorOptions(migrate_successors=False),
    )
    assert (d.verb, d.action) == ("skip", LEFT_ALONE)
    assert "migrate_successors" in d.detail and "switched off" in d.detail


def test_reused_id_is_never_cleared_even_with_everything_gone():
    d = decide(
        orphan(record=removed(id_reused=True), device_exists=False),
        CuratorOptions(clear_unwitnessed=True),
    )
    assert d.verb == "skip" and "mix" in d.detail


def test_reused_id_is_never_migrated_even_with_a_successor():
    d = decide(
        orphan(record=removed(id_reused=True), successor_id=NEW), DEFAULT_OPTIONS
    )
    assert d.verb == "skip"


# --------------------------------------------------------------------------- #
# Migration planning
# --------------------------------------------------------------------------- #


def test_exact_successor_with_free_id_and_fresh_successor_is_a_rename():
    plan = plan_migration(OLD, NEW, removed(), None, series())
    assert plan.method == "rename"


def test_successor_that_already_compiles_gets_an_import_when_series_do_not_overlap():
    s = series(new_exists=True, new_has_sum=False, new_unit="°C", new_first_start=T0 + 3600)
    assert plan_migration(OLD, NEW, removed(), None, s).method == "import"


def test_overlapping_series_are_refused():
    s = series(old_last_start=T0 - 60, new_exists=True, new_has_sum=False, new_unit="°C", new_first_start=T0 - 7200)
    assert plan_migration(OLD, NEW, removed(), None, s).method == "refuse"


def test_running_total_is_never_merged_into_an_existing_total():
    s = series(old_has_sum=True, new_exists=True, new_has_sum=True, new_unit="°C", new_first_start=T0 + 3600)
    assert plan_migration(OLD, NEW, removed(), None, s).method == "refuse"


def test_running_total_may_move_onto_a_successor_with_no_rows():
    s = series(old_has_sum=True, new_exists=True, new_has_sum=True, new_unit="°C", new_first_start=None)
    assert plan_migration(OLD, NEW, removed(), None, s).method == "import"


def test_different_kind_of_statistic_is_refused():
    s = series(new_exists=True, new_has_sum=True, new_unit="°C")
    assert plan_migration(OLD, NEW, removed(), None, s).method == "refuse"


def test_holder_on_the_old_id_refuses_regardless_of_rows():
    plan = plan_migration(OLD, NEW, removed(), ("sensor", "mqtt", "squatter"), series())
    assert plan.method == "refuse" and "squatter" in plan.detail


def test_reused_flag_refuses_even_after_the_holder_is_gone():
    plan = plan_migration(OLD, NEW, removed(id_reused=True), None, series())
    assert plan.method == "refuse" and "reused" in plan.detail


def test_rows_after_the_removal_refuse_even_without_a_known_reuser():
    plan = plan_migration(OLD, NEW, removed(), None, series(old_last_start=T0 + 300))
    assert plan.method == "refuse"


def test_last_bucket_that_started_before_removal_is_allowed():
    plan = plan_migration(OLD, NEW, removed(), None, series(old_last_start=T0 - 1))
    assert plan.method == "rename"


# --------------------------------------------------------------------------- #
# Housekeeping and reporting
# --------------------------------------------------------------------------- #


def test_records_are_pruned_once_their_statistics_are_gone_even_if_the_device_remains():
    records = {
        "sensor.a": removed(device_id="dev1"),
        "sensor.b": removed(device_id="dev2"),
        "sensor.c": removed(device_id=None),
        "sensor.d": removed(device_id="dev4", id_reused=True),
    }
    assert prunable(records, has_statistics={"sensor.a"}) == ["sensor.b", "sensor.c", "sensor.d"]


def test_footer_never_claims_clean_when_a_refusal_is_hidden_from_validation():
    lines = summary_footer([(OLD, LEFT_ALONE, "reused")], remaining=[])
    assert not any("No orphaned statistics remain" in line for line in lines)
    assert any("Repairs" in line for line in lines)


def test_footer_reports_dismissal_only_after_a_clean_settlement_pass():
    assert any("dismissed" in line for line in summary_footer([(OLD, CLEARED, "x")], remaining=[]))
    assert summary_footer([(OLD, MIGRATED, "x")], remaining=None) == []
    assert any(OLD in line for line in summary_footer([], remaining=[OLD]))


def test_footer_does_not_claim_spook_dismissal_when_the_option_is_disabled():
    lines = summary_footer(
        [(OLD, CLEARED, "x")], remaining=[], dismiss_spook_issue=False
    )
    assert any("left open" in line for line in lines)
    assert not any("was dismissed" in line for line in lines)


@pytest.mark.parametrize("flag", [False, True])
def test_prune_keeps_records_while_their_statistics_exist(flag):
    records = {OLD: removed(device_id=None, id_reused=flag)}
    assert prunable(records, has_statistics={OLD}) == []
