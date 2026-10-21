# v1.2.3 (2026-10-20)

- Fix Jellyfin plugin managed dependency loading by listing every packaged SQLite assembly under `meta.json.assemblies` instead of only the plugin DLL.
- Fail packaging if Microsoft.Data.Sqlite, SQLitePCLRaw.core, SQLitePCLRaw.provider.e_sqlite3, SQLitePCLRaw.batteries_v2 or libe_sqlite3.so is absent.
- Verify ZIP content and allowlist before uploading a GitHub Actions artifact.
- Preserve SQLite schema 1, plugin GUID, Composer Browser v3.7.2, and album-credit behavior.
- New release v1.2.3 to avoid overwriting the immutable v1.2.2 GitHub Release.
