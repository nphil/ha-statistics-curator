# Statistics Curator

Settles Home Assistant long-term statistics that no longer have an entity behind
them — the pile Spook reports as *orphaned statistics*.

It never guesses from names or units. It watches entity removals as they happen
and acts only on that evidence:

- **Clears** history whose entity and device are both gone, and history of
  disabled entities
- **Migrates** history when the same device returns under a new entity id
- **Refuses** anything it cannot prove — unwitnessed orphans, a device that is
  still present, a merge that would break a running total
- **Raises its own repair** for an entity id that a different entity later took
  over, because that history is mixed for good and Spook can no longer see it

Every run is reported as a notification, and Spook's repair is retired by letting
Spook re-check, not by deleting the issue behind its back.

Configured entirely in the UI. No YAML.
