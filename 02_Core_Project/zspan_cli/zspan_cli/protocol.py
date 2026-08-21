"""Parse and optionally register ``zspan://meeting/...`` links."""
from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional, Sequence

from zspan_cli import resolver
from zspan_cli.config import zspan_home


SCHEME = "zspan"
_MAC_APP_NAME = "Z-SPAN Handler.app"
_MAC_BUNDLE_ID = "org.zspan.handler"
_MAC_URL_NAME = "org.zspan.meeting"
_LSREGISTER = Path(
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
    "LaunchServices.framework/Support/lsregister"
)
_LINUX_DESKTOP_NAME = "zspan-handler.desktop"


class ProtocolError(Exception):
    """A zspan link or protocol-registration request could not be used."""


def parse_scheme_url(value: object) -> Optional[str]:
    """Return a meeting public id, or None when this is not a zspan URL."""
    if not isinstance(value, str) or not value.lower().startswith(f"{SCHEME}://"):
        return None

    remainder = value[len(SCHEME) + 3 :]
    route, separator, public_id = remainder.partition("/")
    if route.lower() != "meeting":
        raise ProtocolError(
            "this build understands zspan://meeting/… links only."
        )
    if not separator:
        public_id = ""
    if public_id.endswith("/"):
        public_id = public_id[:-1]
    if resolver.PUBLIC_ID_RE.fullmatch(public_id) is None:
        raise ProtocolError(
            "a zspan://meeting/… link must contain a public id like "
            "m_QKQR6sGF6WP5koWphY4zBs, with nothing after it."
        )
    return public_id


def _zspan_invocation() -> list[str]:
    executable = shutil.which("zspan")
    if executable:
        return [executable]
    return [sys.executable, "-m", "zspan_cli"]


def _applescript_source(invocation: Sequence[str]) -> str:
    command = " ".join(shlex.quote(str(part)) for part in invocation)
    literal = command.replace("\\", "\\\\").replace('"', '\\"')
    return (
        "on open location theURL\n"
        f'    do shell script "{literal}" & " open " & quoted form of theURL\n'
        "end open location\n"
    )


def _inject_url_types(info_plist_path: Path) -> None:
    with info_plist_path.open("rb") as handle:
        info = plistlib.load(handle)
    info["CFBundleURLTypes"] = [{
        "CFBundleURLName": _MAC_URL_NAME,
        "CFBundleURLSchemes": [SCHEME],
    }]
    info["CFBundleIdentifier"] = _MAC_BUNDLE_ID
    with info_plist_path.open("wb") as handle:
        plistlib.dump(info, handle)


def _windows_command(invocation: Sequence[str]) -> str:
    if not invocation:
        raise ValueError("the Z-SPAN invocation cannot be empty")
    executable = str(invocation[0]).replace('"', '\\"')
    parts = [f'"{executable}"']
    if len(invocation) > 1:
        parts.append(subprocess.list2cmdline([str(part) for part in invocation[1:]]))
    return " ".join(parts) + ' open "%1"'


def _desktop_entry(invocation: Sequence[str]) -> str:
    command = " ".join(shlex.quote(str(part)) for part in invocation)
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Z-SPAN link handler\n"
        f"Exec={command} open %u\n"
        "MimeType=x-scheme-handler/zspan;\n"
        "NoDisplay=true\n"
        "Terminal=true\n"
    )


def _mac_app_path() -> Path:
    return zspan_home() / _MAC_APP_NAME


def _register_macos(invocation: Sequence[str]) -> str:
    home = zspan_home()
    final_app = _mac_app_path()
    try:
        home.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="protocol-", dir=home) as temp_name:
            temp_dir = Path(temp_name)
            source_path = temp_dir / "handler.applescript"
            built_app = temp_dir / _MAC_APP_NAME
            backup_app = temp_dir / "previous-handler.app"
            source_path.write_text(
                _applescript_source(invocation), encoding="utf-8"
            )
            subprocess.run(
                ["/usr/bin/osacompile", "-o", str(built_app), str(source_path)],
                check=True,
                capture_output=True,
                text=True,
            )
            _inject_url_types(built_app / "Contents" / "Info.plist")

            replaced_existing = final_app.exists()
            if replaced_existing:
                os.replace(final_app, backup_app)
            try:
                os.replace(built_app, final_app)
            except OSError:
                if replaced_existing and backup_app.exists():
                    os.replace(backup_app, final_app)
                raise
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException) as exc:
        raise ProtocolError(
            f"could not create the macOS Z-SPAN handler at {final_app}: {exc}"
        ) from exc

    refreshed = False
    if _LSREGISTER.is_file():
        try:
            result = subprocess.run(
                [str(_LSREGISTER), "-f", str(final_app)],
                check=False,
                capture_output=True,
                text=True,
            )
            refreshed = result.returncode == 0
        except OSError:
            pass
    if refreshed:
        return f"Registered zspan:// links with {final_app}."
    return (
        f"Created {final_app}; open it once manually so macOS registers "
        "zspan:// links."
    )


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    else:
        shutil.rmtree(path)


def _unregister_macos() -> str:
    app_path = _mac_app_path()
    if not app_path.exists() and not app_path.is_symlink():
        return f"Nothing was registered at {app_path}."
    try:
        _remove_path(app_path)
    except OSError as exc:
        raise ProtocolError(
            f"could not remove the macOS Z-SPAN handler at {app_path}: {exc}"
        ) from exc
    return f"Removed the macOS Z-SPAN handler at {app_path}."


def _register_windows(invocation: Sequence[str]) -> str:
    try:
        import winreg

        key_path = r"Software\Classes\zspan"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as scheme_key:
            winreg.SetValueEx(
                scheme_key, "", 0, winreg.REG_SZ, "URL:Z-SPAN meeting link"
            )
            winreg.SetValueEx(scheme_key, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER, key_path + r"\shell\open\command"
        ) as command_key:
            winreg.SetValueEx(
                command_key, "", 0, winreg.REG_SZ, _windows_command(invocation)
            )
    except (ImportError, OSError) as exc:
        raise ProtocolError(
            "could not register zspan:// links in the current user's Windows "
            f"registry: {exc}"
        ) from exc
    return r"Registered zspan:// links under HKCU\Software\Classes\zspan."


def _unregister_windows() -> str:
    try:
        import winreg

        key_path = r"Software\Classes\zspan"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path):
                pass
        except FileNotFoundError:
            return r"Nothing was registered under HKCU\Software\Classes\zspan."
        for suffix in (
            r"\shell\open\command",
            r"\shell\open",
            r"\shell",
            "",
        ):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path + suffix)
    except (ImportError, OSError) as exc:
        raise ProtocolError(
            "could not remove the current user's zspan:// Windows registry "
            f"keys: {exc}"
        ) from exc
    return r"Removed zspan:// registration from HKCU\Software\Classes\zspan."


def _linux_desktop_path() -> Path:
    try:
        home = Path.home()
    except RuntimeError as exc:
        raise ProtocolError(
            "could not find a home directory for Linux protocol registration."
        ) from exc
    if not home.is_absolute():
        raise ProtocolError(
            "could not find a home directory for Linux protocol registration."
        )
    return home / ".local" / "share" / "applications" / _LINUX_DESKTOP_NAME


def _run_optional(command: Sequence[str]) -> bool:
    executable = shutil.which(str(command[0]))
    if not executable:
        return False
    try:
        result = subprocess.run(
            [executable, *[str(part) for part in command[1:]]],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return result.returncode == 0


def _register_linux(invocation: Sequence[str]) -> str:
    desktop_path = _linux_desktop_path()
    try:
        desktop_path.parent.mkdir(parents=True, exist_ok=True)
        desktop_path.write_text(_desktop_entry(invocation), encoding="utf-8")
    except OSError as exc:
        raise ProtocolError(
            f"could not write the Linux desktop entry at {desktop_path}: {exc}"
        ) from exc

    mime_set = _run_optional([
        "xdg-mime", "default", _LINUX_DESKTOP_NAME, "x-scheme-handler/zspan"
    ])
    database_updated = _run_optional([
        "update-desktop-database", str(desktop_path.parent)
    ])
    if mime_set and database_updated:
        return f"Registered zspan:// links with {desktop_path}."
    return (
        f"Desktop file written at {desktop_path}; your desktop environment "
        "may need xdg-utils to route zspan:// links."
    )


def _unregister_linux() -> str:
    desktop_path = _linux_desktop_path()
    if not desktop_path.exists():
        return f"Nothing was registered at {desktop_path}."
    try:
        desktop_path.unlink()
    except OSError as exc:
        raise ProtocolError(
            f"could not remove the Linux desktop entry at {desktop_path}: {exc}"
        ) from exc
    _run_optional(["update-desktop-database", str(desktop_path.parent)])
    return (
        f"Removed {desktop_path}; any xdg-mime default residue clears once "
        "the missing desktop file is noticed."
    )


def _unsupported_platform() -> ProtocolError:
    return ProtocolError(
        f"protocol registration is not supported on platform {sys.platform!r} "
        "by this build."
    )


def register() -> str:
    """Opt in to per-user zspan:// URL handling on the current platform."""
    invocation = _zspan_invocation()
    if sys.platform == "darwin":
        return _register_macos(invocation)
    if sys.platform == "win32":
        return _register_windows(invocation)
    if sys.platform.startswith("linux"):
        return _register_linux(invocation)
    raise _unsupported_platform()


def unregister() -> str:
    """Remove the per-user zspan:// handler artifact for this platform."""
    if sys.platform == "darwin":
        return _unregister_macos()
    if sys.platform == "win32":
        return _unregister_windows()
    if sys.platform.startswith("linux"):
        return _unregister_linux()
    raise _unsupported_platform()
