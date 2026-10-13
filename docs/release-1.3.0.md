# ConfigBackup 1.3.0

The configurator now offers a full-screen keyboard interface with colored menus, arrow navigation, filtering, Space-to-toggle checklists, editable fields and a scrollable results panel. Main-menu shortcuts provide Save, Validate and Preview changes. Existing safe-save and secret-handling workflows remain in place.

Install `requirements-console.txt` alongside the standard requirements, then run `configure.py` normally. Use `--plain` for the previous numbered menus. See the [console guide](console-configurator.md).

Automated tests cover keyboard dispatch, real wizard editing/saving, cancellation, confirmation, screen content and external-operation return. Native Windows terminal behavior remains unverified. The Windows kit retains its 283 application fixtures and GPO harness 1.0.0; optional console tests are part of the full application suite.

The loaded configuration is inventoried and can be edited through a nested settings browser. A dependency menu offers explicitly approved install/update commands for the current Python environment, and missing timezone data has a dedicated repair message.
