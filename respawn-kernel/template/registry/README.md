# Source registry boundary

Public coverage status and non-sensitive source citations may be tracked in `data/sources.json`.

Executable site recipes, credentials, discovery notes that create bulk-acquisition risk, and other sensitive collection details belong in `registry/private/`. That directory is ignored by Git and remains under the local maintainer’s custody.

Do not store secrets in this directory’s tracked files.
