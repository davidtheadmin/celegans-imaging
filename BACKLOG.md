# Polish backlog

Small items deferred from main work. Pick up when convenient.

## UI

- **Soft-deleted thumbnails don't vanish from timeline.** When a file is
  deleted in the timeline, the thumbnail is correctly marked "deleted" but
  remains visible. Should fade out / be removed from the strip after delete.

- **Motility assay nomenclature.** Subgroups within a motility session are
  currently labeled "plates" (inherited from the survival assay schema).
  Should be "videos" or "captures" — plates make no sense for motility.
  Check both the frontend labels and any session.json schema fields that
  surface to the user.
