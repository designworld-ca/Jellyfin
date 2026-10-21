# Composer Shared Metadata v1.2.3 — Linux x64 packaging fix

This complete source release repairs the **SQLite managed-assembly loading failure** reported after upgrading to plugin v1.2.2.

**No SQLite schema change.** Composer Browser v3.7.2 and plugin GUID `4c65902b-e89d-4549-a313-2a204c622e77` are unchanged. The error `CSM510: Could not load file or assembly Microsoft.Data.Sqlite, Version=10.0.13.0` was caused by a plugin runtime dependency-loading issue. The existing v1.2.2 `meta.json` whitelisted only the main plugin DLL, even though the packaging script included dependent assemblies. This version adds all managed SQLite DLLs to Jellyfin's plugin assembly whitelist and verifies the ZIP before publishing.

## Install safely

1. Back up the **entire directory** `/home/roo/runtipi/app-data/migrated/jellyfin/data/config/data/plugins/configurations/ComposerSharedMetadata/` while preserving the database and any `-wal`/`-shm` files. For a consistent snapshot, use SQLite's online backup or stop Jellyfin before copying. Never delete it during upgrade.
2. Merge the files from this ZIP into the root of your [Composer_Metadata GitHub repository](https://github.com/designworld-ca/Composer_Metadata), preserving `.github/` and `plugin/` paths. Review local modifications first. Commit and push.
3. Run **Build native Composer plugin**. It must publish a Linux x64 artifact whose ZIP `meta.json` is version `1.2.3.0` and whose `assemblies` array lists `Microsoft.Data.Sqlite.dll` and `SQLitePCLRaw` dependencies. The build now checks ZIP structure.
4. Run **Publish Composer repository manifest**. It publishes a new release **v1.2.3**, not an overwrite of v1.2.2.
5. In Jellyfin, update the plugin from Catalog, restart Runtipi Jellyfin, then confirm `api.getJSON(api.getUrl("ComposerSharedMetadata/health"))` reports `pluginVersion: "1.2.3"`.
6. Verify `api.getJSON(api.getUrl("ComposerSharedMetadata/records"))` loads the saved 8 records. Only then inspect the composer portraits. No browser update required.

## Additional diagnostics (read-only)

```bash
# On the Runtipi host, find the installed plugin binaries. No files modified.
sudo find /home/roo/runtipi/app-data/migrated/jellyfin/data/config/data/plugins \
  -maxdepth 3 -type f \( -name 'Microsoft.Data.Sqlite.dll' -o -name 'SQLitePCLRaw*.dll' -o -name 'meta.json' \) -print
```

You can also download the actual v1.2.3 installation ZIP from GitHub Releases and run:

```bash
python3 tools/verify_composer_install_zip.py \
  --archive jellyfin-composer-shared-metadata-1.2.3-linux-x64-install.zip \
  --expected-version 1.2.3
```

**Known validation limitation:** This workspace cannot run .NET SDK compilation or inspect your live Jellyfin server. Static ZIP checks and simulated packager tests cannot prove the server will load the plugin. If CSM510 persists, inspect the installed plugin directory and its managed DLLs. Do not touch SQLite files.
