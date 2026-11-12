"""TomTom API key lookup.

Spec: return the key from the process environment, else from the Windows user
environment (``HKCU\\Environment``), else ``None``. The value is never logged or
returned by any route; callers only ever expose ``bool(read_api_key())``.
"""
from __future__ import annotations

import os


def read_api_key():
    """Read process then Windows User configuration without exposing the value."""
    key = os.environ.get("TOMTOM_API_KEY", "").strip()
    if key:
        return key
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
                value, _ = winreg.QueryValueEx(handle, "TOMTOM_API_KEY")
                return str(value).strip() or None
        except OSError:
            pass
    return None
