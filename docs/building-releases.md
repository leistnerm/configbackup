# Building release packages

From a clean Git checkout with Git installed, run Python 3.10 or newer:

```sh
python tools/build_release.py
```

The builder reads the application version without importing the engine and packages
only paths tracked in the Git index. Untracked local configuration, credentials and
scratch files are omitted. Stage new source files before a local build; tracked
files that are missing or symbolic links cause the build to fail. It reads current
working-tree bytes, so use a clean checkout for publication.

The standard-library-only builder writes application, standalone GPO harness and Windows workstation kit ZIPs plus SHA-256 sidecars to `dist/`. It uses sorted entries, fixed timestamps and file modes, rebuilds each ZIP to check byte equality, and checks archive CRCs. The kit includes the exact application and GPO ZIPs, with generated hash pins and a complete file manifest. The builder never uploads or modifies Git.

The application ZIP excludes release tooling and kit source. Build from the full Git checkout, not from an extracted application ZIP. Use `--output /another/directory` to choose an output directory outside the repository. ZIP compression can vary across Python/zlib versions; compare using the same build environment. Checksums detect corruption and changes, not publisher authenticity.

After installing the core and optional requirements, run application tests from the repository root:

```sh
python -m unittest discover -s tests -v
```

PowerShell and Git enable additional fixtures. Review skipped tests and platform limitations in `VALIDATION.md`. Extract the workstation kit and exercise its offline launcher before publication; this checks the generated inner archives and their pins. Native Windows acceptance is a separate local activity.
