"""Opt-in prerequisite setup. Nothing runs on import; every install needs consent."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

REQUIREMENTS = ["PyYAML>=6.0,<7", "tzdata>=2024.1", "ruamel.yaml>=0.18.6,<0.19"]


def choice(prompt, allowed, ask=input):
    try:
        value = ask(prompt).strip().casefold()
    except (EOFError, KeyboardInterrupt):
        return "n"
    return value if value in allowed else "n"


def pip_arguments(python, wheelhouse=None):
    args = [str(python), "-I", "-m", "pip", "install", "--disable-pip-version-check",
            "--no-input", "--only-binary=:all:"]
    if wheelhouse is not None:
        args += ["--no-index", "--find-links", str(wheelhouse)]
    return args + REQUIREMENTS


def refresh_git_path():
    """Only refresh this process's lookup; never edit persistent PATH/registry."""
    if os.name != "nt":
        return
    import winreg
    entries = []
    for hive, key in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                      (winreg.HKEY_CURRENT_USER, "Environment")):
        try:
            with winreg.OpenKey(hive, key) as handle:
                value, _ = winreg.QueryValueEx(handle, "Path")
                entries.extend(os.path.expandvars(value).split(os.pathsep))
        except OSError:
            pass
    for name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(name)
        if base:
            entries.append(str(Path(base) / ("Programs/Git/cmd" if name == "LOCALAPPDATA" else "Git/cmd")))
    current = os.environ.get("PATH", "").split(os.pathsep)
    for entry in entries:
        if entry and entry not in current:
            current.append(entry)
    os.environ["PATH"] = os.pathsep.join(current)


def prepare(output, inspect, record, *, python=None, wheelhouse=None,
            ask=input, execute=None, locate=shutil.which, windows=None, refresh=refresh_git_path):
    """Return the interpreter to use, recording consent/attempts locally.

    inspect(executable) starts a fresh dependency probe. record(event) checkpoints
    the report. Injected functions permit simulations without real installers.
    """
    output = Path(output)
    selected = str(python or sys.executable)
    windows = os.name == "nt" if windows is None else windows
    if not windows:
        record(dict(name="Prerequisite setup", status="NOT RUN", detail="Automatic setup is offered only on Windows."))
        return selected

    def run(args, log_name=None):
        if execute is not None:
            return execute(args, log_name)
        env = dict(os.environ)
        scratch = output / "setup-scratch"
        scratch.mkdir(exist_ok=True)
        env.update(TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch))
        # Carry only connection settings, never a target/prefix/user override.
        # Disable pip configuration files, which can also redirect installation.
        connection_keys = {"PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_CERT", "PIP_CLIENT_CERT", "PIP_PROXY"}
        for key in list(env):
            if key.upper().startswith("PIP_") and key.upper() not in connection_keys:
                del env[key]
        env["PIP_CONFIG_FILE"] = os.devnull
        if log_name:
            with (output / log_name).open("wb") as log:
                return subprocess.run(args, cwd=output, env=env, stdout=log, stderr=subprocess.STDOUT,
                                      shell=False, timeout=600).returncode
        # WinGet/installer license and UAC prompts remain visible to the user.
        return subprocess.run(args, cwd=output, env=env, shell=False).returncode

    before = inspect(selected)
    if not all(row["status"] == "PASS" for row in before["checks"]):
        print("\nMissing Python packages/time-zone data.")
        print("Optional: create a NEW test environment in this report folder and install PyYAML 6.x, ruamel.yaml 0.18.x and tzdata.")
        print("Your existing Python environment will not be modified. Online mode uses the default index or approved index/proxy environment settings.")
        print("Local-wheel mode disables package indexes. Nothing downloads or installs unless you choose it.")
        answer = choice("Install: [O] online, [L] local approved wheels, [N] skip (default N): ", {"o", "l", "n"}, ask)
        if answer == "n":
            record(dict(name="Python packages", status="DECLINED", detail="No environment created or packages installed."))
        else:
            environment = output / "test-environment"
            try:
                wheels = None
                if answer == "l":
                    value = wheelhouse or ask("Approved LOCAL wheel folder (blank cancels): ").strip().strip('"')
                    if not value:
                        raise ValueError("No wheel folder selected; setup cancelled")
                    if str(value).startswith(("\\\\", "//")):
                        raise ValueError("Use a local wheel folder; network paths are not accepted")
                    wheels = Path(value).expanduser().resolve()
                    if not wheels.is_dir():
                        raise ValueError("The wheel folder does not exist")
                    if os.name == "nt":
                        import ctypes
                        if ctypes.windll.kernel32.GetDriveTypeW(str(wheels.anchor)) not in (2, 3, 6):
                            raise ValueError("Use a local wheel folder, not a mapped network drive")
                environment.mkdir(exist_ok=False)
                record(dict(name="Python packages", status="STARTED", detail="Approved " + ("offline wheels" if wheels else "default/environment package index") + "; new environment: " + str(environment)))
                args = [selected, "-I", "-m", "venv", str(environment)]
                code = run(args, "setup-venv.log")
                if code:
                    raise RuntimeError("venv creation failed (exit %s); see setup-venv.log" % code)
                target = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                code = run(pip_arguments(target, wheels), "setup-pip.log")
                if code:
                    raise RuntimeError("pip failed (exit %s); see setup-pip.log. Existing Python retained." % code)
                after = inspect(str(target))
                if not all(row["status"] == "PASS" for row in after["checks"]):
                    raise RuntimeError("Package installation returned success but fresh dependency checks did not pass")
                selected = str(target)
                record(dict(name="Python packages", status="VERIFIED", detail="Fresh interpreter probe passed; tests will use the new isolated environment."))
            except (Exception, KeyboardInterrupt) as exc:
                record(dict(name="Python packages", status="FAILED", detail=str(exc) or type(exc).__name__))

    if not locate("git"):
        winget = locate("winget")
        if not winget:
            record(dict(name="Git installation", status="UNAVAILABLE", detail="WinGet is unavailable; use your approved Git installer. Fifteen Git checks will remain NOT RUN if Git is still unavailable."))
        else:
            args = [winget, "install", "--id", "Git.Git", "--exact", "--source", "winget", "--interactive", "--no-upgrade"]
            print("\nGit is missing. It is used by 15 selected synthetic local tests.")
            print("Optional command: winget install --id Git.Git --exact --source winget --interactive --no-upgrade")
            print("This downloads Git and launches its installer; installation may be machine-wide and request UAC.")
            print("Review the installer and license/source prompts. No reboot or agreement bypass is requested.")
            if choice("Install Git now? [y/N]: ", {"y", "n"}, ask) != "y":
                record(dict(name="Git installation", status="DECLINED", detail="No Git installer started."))
            else:
                record(dict(name="Git installation", status="STARTED", detail="User approved the displayed WinGet command."))
                try:
                    code = run(args)
                    refresh()
                    executable = locate("git")
                    if not executable:
                        raise RuntimeError("WinGet exit %s; Git is not discoverable. Restart the launcher after completing your approved installation." % code)
                    probe = run([executable, "--version"], "setup-git-probe.log")
                    if probe:
                        raise RuntimeError("Git version probe failed (exit %s)" % probe)
                    record(dict(name="Git installation", status="VERIFIED" if code == 0 else "REVIEW",
                                detail="WinGet exit %s; fresh Git execution succeeded. Test prerequisites will be checked again." % code))
                except (Exception, KeyboardInterrupt) as exc:
                    record(dict(name="Git installation", status="FAILED", detail=str(exc) or type(exc).__name__))
    return selected
