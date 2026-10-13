# ConfigBackup GPO test 1.0.0

A standalone, local test of the **ConfigBackup 1.0.0 GPO collector**. It exports existing effective computer and user policy results twice, checks file hashes and repeatability, and writes an offline HTML report for you to review. Reports remain local and can contain sensitive configuration data.

## Run on your Windows computer

1. Extract the entire ZIP into a local, nonsynced folder. Keep its files together.
2. Use an existing compatible **Python 3.10 or newer** installation. No pip packages, PyYAML, RSAT, domain-admin rights, or internet connection for installation are required by this bundle. Windows must have `gpresult.exe`. Existing system access controls still apply.
3. Open **PowerShell as Administrator**, and change into the extracted folder:

   ```powershell
   cd 'C:\Tools\configbackup-gpo-test-1.0.0'
   py -3 .\test_gpo_collection.py
   ```

   The Python command works without changing PowerShell's script execution policy. If you prefer the wrapper and your existing policy permits running it:

   ```powershell
   .\Test-GpoCollection.ps1
   ```

   If `py` is unavailable, call your existing Python directly:

   ```powershell
   & 'C:\Path\To\python.exe' .\test_gpo_collection.py
   ```

4. Wait for the printed result. It normally takes two exports per scope plus a 15-second pause; each export can take up to 120 seconds. Open **`report.html` in the printed output folder**. By default this is a fresh subfolder of `%LOCALAPPDATA%\ConfigBackup\GpoTests`. No browser is opened automatically.
5. Follow [REVIEW-GUIDE.txt](REVIEW-GUIDE.txt). Confirm the account, computer, every scope's status, expected policy coverage, and any normalized differences. Keep the report on your computer.

### If elevation uses a different admin account

The default user export belongs to the account running Python. Add your usual account explicitly, without a password:

```powershell
py -3 .\test_gpo_collection.py --user 'DOMAIN\yourname'
# Or, using the wrapper:
.\Test-GpoCollection.ps1 -TargetUser 'DOMAIN\yourname'
```

This adds a separate user scope; it keeps the process account's scope too. If that admin account has no RSoP data, its failure remains visible as INCOMPLETE even when your normal account succeeds. `/user` selects whose existing RSoP to read; it does not log in or impersonate that account. More than one `--user` is supported (maximum 10).

### More samples or a chosen local folder

```powershell
py -3 .\test_gpo_collection.py --runs 3 --interval 30 --output 'C:\LocalTests\Gpo-FirstCheck'
```

Use a **new** output directory. Existing directories, UNC paths, and mapped network drives are refused. Local folders can still be synced by third-party software; select a nonsynced location. The script does not alter ACLs or turn sync off. Files inherit the destination's permissions.

## What the script does

- Pins the Windows system `gpresult.exe`, avoiding executable lookup through PATH.
- Runs only local `gpresult /scope computer|user /x <fresh-temp-file> /f`, with optional `/user <account>`. `/f` overwrites only a new temporary export, not a policy.
- Records the process account/SID using the system `whoami.exe /user /fo csv /nh`, plus OS/Python/encoding information and elevation state.
- Runs the unchanged `collect_rsop`, `normalize_rsop`, and scope-hashing functions bundled from ConfigBackup 1.0.0. Source hashes are verified before collection.
- Validates expected scope XML, both configuration file hashes, raw-to-normalized consistency, and normalization idempotence. Compares every successful scope's configuration files byte-for-byte by SHA-256 against run 1.
- Keeps failed scope diagnostics and continues other scopes and runs. Malformed XML, timeouts, disabled scopes, or missing data cannot produce PASS.
- Saves original XML, normalized XML, per-run records, command diagnostics, a JSON summary, HTML, and complete order-sensitive XML path diffs when configuration changes.

The test invokes **no `gpupdate`, policy changes, registry writes, scheduled tasks, Git, SMTP, HTTP, uploads, remote-computer queries, or backup cleanup**. Windows tools may use normal OS/domain services while querying existing RSoP; this is not a promise to disable Windows networking or background policy refresh. The harness writes only its report folder and disposable export directories inside it. Launching Python/PowerShell and querying Windows may also produce normal OS logs/caches.

## Reading the result

| Result | Meaning | Exit code |
| --- | --- | --- |
| PASS | Every expected scope was usable on every run; certified configuration files matched. Check identity and expected policy coverage manually. | 0 |
| REVIEW | Successful configuration output changed. Inspect the local diffs; a background refresh may explain it. | 2 |
| INCOMPLETE | A scope failed, was disabled, missing, invalid, or the run stopped early. Successful scopes still have individual results. | 3 |

Argument/setup errors exit 2 before a report exists. Unexpected runtime/system failures can stop report creation; a missing report is not a passing test.

Raw XML hash changes alone need not indicate configuration changes. The existing normalizer removes root `CreationTime` and uses XML canonicalization. It preserves policy modification dates, extension data, element order, and whitespace. It does not blindly remove metadata or sort policy lists. XML parser/canonicalization behavior, including discarded XML comments and processing instructions, is that of the existing collector. The source is included for inspection.

The XML checks establish basic structure and repeatability, **not complete organizational policy coverage**. GPO node counts are not applied-policy counts. This tests effective local RSoP, not domain-wide GPO definitions, SYSVOL, full Intune/MDM inventory, or ConfigBackup's archive/Git/deletion pipeline.

## Source provenance and test boundaries

`collector-source/` contains byte-for-byte copies of three ConfigBackup 1.0.0 source files. `collector_adapter.py` checks pinned SHA-256 values and compiles only the six named functions listed there. This avoids importing the registry/YAML collector and keeps this bundle standard-library-only. Function bodies are unchanged. Injected subprocess and temporary-directory adapters restrict commands, log diagnostics, and keep exports inside the fresh local report directory; XML over 64 MiB and DTD/entity declarations are rejected before the original normalizer runs.

Inherited `CONFIGBACKUP_SECTIONS` switches are honored by the original function and surfaced as disabled/incomplete. This is an isolated collector test: it does not read your full ConfigBackup configuration or update existing snapshots. An explicit `--user` selection is represented by a deterministic hashed scope name; its account name is in local `context.json` and the report.

To run the offline fixture tests:

```powershell
py -3 -m unittest discover -s tests -v
```

Fixtures use synthetic data and do not query the OS. See [VALIDATION.txt](VALIDATION.txt) for coverage and limitations. Live Windows collection must be evaluated on the machine where you run it; no live Windows success is claimed by this bundle.

Microsoft documents the read-only reporting switches and RSoP behavior in the [gpresult command reference](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/gpresult). XML (`/x`) is used here; the Microsoft ARM64 caveat concerning the HTML (`/h`) executable path is not exercised by this script.
