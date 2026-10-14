# nd_installed

Plugins of the ND suite whose source is not in `nd_claude_1.23/`.

- `nd_data_updater/` is the ND Data Updater **1.4.1** as installed in the ND QGIS profile, with the
  2026-10-14 security fixes applied (auto-update off by default, NSI generator pinned to a commit
  with a SHA-256 check, SHA-256 recorded for every download, fallback when Geofabrik publishes no
  .md5, digest cache in `ndpack/damsuite/chain.py`). RELEASE_1.23.md lists the updater at 1.5.0,
  but that source was not available, so it is kept here rather than under `nd_claude_1.23/`, where
  it would carry the wrong release number.
