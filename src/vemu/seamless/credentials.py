"""Optional OS keyring storage for the Windows RDP password.

The password is never written to the seamless state file or a shortcut.  If
the keyring is unavailable the caller must prompt each time.
"""

from __future__ import annotations

from ..errors import VemuError

SERVICE = "vemu-seamless"


def account(domain: str, username: str) -> str:
    return f"{domain}|{username}"


def _load_keyring():
    try:
        import keyring
    except ImportError as exc:
        raise VemuError(
            "Python keyring support is not installed; install the 'keyring' "
            "extra or enter the password each time."
        ) from exc
    return keyring


def available() -> bool:
    try:
        _load_keyring()
    except VemuError:
        return False
    return True


def save_password(domain: str, username: str, password: str) -> bool:
    if not password:
        raise VemuError("A non-empty password is required.")
    keyring = _load_keyring()
    try:
        keyring.set_password(
            SERVICE, account(domain, username), password
        )
    except Exception as exc:  # keyring backends raise several types
        raise VemuError(f"Cannot save the password in the keyring: {exc}") from exc
    return True


def get_password(domain: str, username: str) -> str | None:
    keyring = _load_keyring()
    try:
        value = keyring.get_password(SERVICE, account(domain, username))
    except Exception as exc:
        raise VemuError(f"Cannot read the password from the keyring: {exc}") from exc
    return value or None


def delete_password(domain: str, username: str) -> bool:
    try:
        keyring = _load_keyring()
    except VemuError:
        return False
    service = SERVICE
    key = account(domain, username)
    try:
        active = keyring.get_keyring()
    except Exception:
        active = keyring
    backends = getattr(active, "backends", None)
    if isinstance(backends, (list, tuple)) and backends:
        # ChainerBackend deletes only from its first backend. A password saved
        # through SecretService can therefore be readable but undeletable when
        # KWallet is also present. Try every backend that has the entry.
        removed = False
        for backend in backends:
            try:
                if backend.get_password(service, key) is None:
                    continue
                backend.delete_password(service, key)
                removed = True
            except Exception:
                continue
        return removed
    try:
        keyring.delete_password(service, key)
    except Exception:
        return False
    return True
