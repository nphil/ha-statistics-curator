<img src="custom_components/statistics_curator/brand/icon.png" width="96" align="right" alt="">

# Statistics Curator

Settles Home Assistant long-term statistics that no longer have an entity behind
them — the pile that Spook reports as *orphaned statistics* and that Home
Assistant itself only lets you clear one row at a time.

It never guesses. Every action is backed by something it watched happen.

[![hacs][hacs-badge]][hacs] [![release][release-badge]][releases]

## Why this exists

Delete a device and its recorded history stays behind forever. Home Assistant
notices (`validate_statistics` reports `no_state`) but does nothing, and the only
UI for it is Settings → Tools → Statistics, one id at a time. Spook surfaces the
list; nothing settles it.

Settling it *safely* is the hard part, because "orphaned" covers three very
different situations that look identical in the database:

| What actually happened | Right answer |
|---|---|
| Device is gone for good | delete the history |
| Same device came back with a new entity id | move the history onto it |
| Something else took over that entity id | neither — the history is now mixed |

Nothing in the database distinguishes them after the fact: the entity registry
forgets an identity the moment it is re-created, and matching on names or units
would happily graft one device's history onto another. So this integration
**watches removals as they happen** and keeps the evidence.

## What it does

- **Clears** statistics whose entity was witnessed being removed *and* whose
  device is gone too, and statistics belonging to a disabled entity (a disabled
  entity strands its history exactly like a deleted one).
- **Migrates** statistics when the exact same `(domain, platform, unique_id)`
  reappears under a different entity id — renaming the series, or merging it
  into the successor when that is provably safe.
- **Refuses, loudly**, everything it cannot prove: an orphan whose removal it
  never saw, an entity removed while its device is still present, a series that
  would collide with a successor's running total, and any entity id that a
  *different* identity later took over.
- **Reports** every run as a persistent notification, and raises a dedicated
  repair for mixed history that Spook can no longer see.
- **Retires Spook's repair** by reloading Spook and letting its own inspection
  clear it, rather than deleting the issue behind its back.

### Reused entity ids get their own repair

If a different entity takes over a removed entity's id, that series is mixed for
good: statistics rows are bucketed, so the newcomer's data lands in the same
5‑minute and hourly rows that were open when the old entity left. No timestamp
can separate them again.

Such a series is never migrated and never cleared automatically. And because the
newcomer has a live state, Spook stops reporting it — the problem becomes
invisible. So this integration raises its own persistent repair, with a **Fix**
button that clears the mixed series once you have decided neither device needs
it.

## Installation

### HACS

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/nphil/ha-statistics-curator`, category **Integration**.
2. Install **Statistics Curator**, restart Home Assistant.
3. Settings → Devices & services → **Add integration** → *Statistics Curator*.

### Manual

Copy `custom_components/statistics_curator/` into your `config/custom_components/`,
restart, then add the integration from the UI.

## Configuration

Everything is configured in the UI — there is no YAML. Settings → Devices &
services → Statistics Curator → **Configure**:

| Option | Default | What it does |
|---|---|---|
| Clear statistics of disabled entities | on | A disabled entity keeps no history; clear it |
| Migrate to a renamed successor | on | Move history when the same device returns under a new entity id |
| Clear history of deleted devices | on | Clear when both the entity and its device are gone |
| Clear orphans that were never witnessed | **off** | History predating this integration, or removals it did not see. Nothing proves those devices are gone — leave it off unless you know |
| Delay before acting | 30 s | Settling time after Spook raises its repair, so a device being re-paired is not mistaken for a deletion |
| Let Spook retire its repair | on | Reload Spook so its own re-check clears the issue |

Turning an option off does not hide the orphan: it is still reported, with the
reason it was left alone.

### Service

`statistics_curator.curate` runs a pass immediately. Its optional
`clear_unwitnessed` field overrides the option for that one call — the supported
way to settle old, unexplained history deliberately.

## How it decides

```
orphaned statistic
├─ entity exists?
│  ├─ disabled ──────────────────────────────► clear
│  └─ enabled (integration not loaded) ──────► leave alone
└─ no entity
   ├─ id later taken by another identity ────► leave alone + raise repair
   ├─ exact identity live under a new id ────► migrate (rename, or safe merge)
   ├─ removal witnessed, device also gone ───► clear
   ├─ removal witnessed, device still here ──► leave alone, ask you to delete it
   └─ removal never witnessed ───────────────► leave alone (unless you opt in)
```

Removal evidence lives in `.storage/statistics_curator.removed_entities`:
identity, device, and when it happened. Records are forgotten once no statistics
remain under their id.

## Requirements

- Home Assistant **2026.9.0** or newer, with the `recorder` integration (default).
- [Spook][spook] is optional. Without it you drive the integration with the
  service; with it, runs are triggered automatically.

## Development

Decisions live in `custom_components/statistics_curator/logic.py` as pure
functions with no Home Assistant imports, so they are testable without a Core
checkout:

```bash
python3 -m pytest tests
```

## Licence

MIT © nphil

[hacs]: https://github.com/hacs/integration
[hacs-badge]: https://img.shields.io/badge/HACS-custom-41BDF5.svg
[release-badge]: https://img.shields.io/github/v/release/nphil/ha-statistics-curator
[releases]: https://github.com/nphil/ha-statistics-curator/releases
[spook]: https://spook.boo
