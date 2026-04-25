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

## Launcher mirror folder structure

Current mirror layout uses Pi-internal names that are fine for code but
unfriendly when browsing in Explorer:
Documents\WormScan
├── freecapture
└── sessions<session_id_timestamp>\plates<folder_name>\

Desired layout, mapped at the launcher (Pi stays as-is):
Documents\WormScan
├── Free captures
└── <user-given session name>
├── WT 0J
├── WT 10J
└── ...

Means:
- Rename `freecapture` → `Free captures` (or similar) at mirror time.
- Rename `sessions` top-level away (move user sessions to top level).
- Use the user-given session name (from session.json) instead of the
  generated session_id in the path.
- Use the condition name as the second-level folder (drop the
  intermediate `plates/` segment).

Note: the launcher's recovery logic relies on Pi-relative paths matching
the mirror structure. Renaming requires a stable mapping table held in
the launcher, OR the launcher writes a `.wormscan-meta` marker per
folder recording the original Pi path. Decide which when we pick this up.

## UI: delete sessions and conditions, not just plates

Currently the timeline only supports deleting individual plates (and free
captures). Need bulk-delete operations:

- Delete entire session: removes all plates + session.json. Soft-delete
  to .trash/sessions/<sid>/ for recoverability.
- Delete a condition within a session: removes all plates assigned to
  that condition, but leaves the rest of the session intact.

Both need confirmation dialogs ("Delete session 'UV survival run 3' and
all 60 plates?"). Soft-delete semantics should match the existing
per-plate delete so retention can clean up later.
