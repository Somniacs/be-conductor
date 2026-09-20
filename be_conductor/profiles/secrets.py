# be-conductor — Local orchestration for terminal sessions.
#
# Copyright (c) 2026 Max Rheiner / Somniacs AG
#
# Licensed under the MIT License. You may obtain a copy
# of the license at:
#
#     https://opensource.org/licenses/MIT
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND.

"""Secret storage for profiles — OS keyring, with a 0600 file fallback.

A secret is addressed by a ``service/username`` reference (the ``keyring:``
value in a profile's ``secrets`` entry).  Values never go to config.yaml.
The file fallback exists for headless Linux boxes without a Secret Service;
``backend_name()`` lets the dashboard warn when it is in use.
"""

import logging
import os
import re
import sys

from be_conductor.utils.config import SECRETS_DIR

log = logging.getLogger(__name__)

_SAFE_REF = re.compile(r"[^A-Za-z0-9_.-]+")


def _split_ref(ref: str) -> tuple[str, str]:
    service, _, username = ref.partition("/")
    return (service or "be-conductor", username or "default")


def _keyring():
    """Return the keyring module if a real backend is available, else None."""
    try:
        import keyring
        from keyring.backends import fail
    except Exception:
        return None
    try:
        backend = keyring.get_keyring()
    except Exception:
        return None
    if isinstance(backend, fail.Keyring):
        return None
    # The chainer reports itself as viable even when every backend under it
    # is the fail backend — look inside.
    inner = getattr(backend, "backends", None)
    if inner is not None and not list(inner):
        return None
    return keyring


def backend_name() -> str:
    """'keyring' when the OS keyring is usable, else 'file'."""
    return "keyring" if _keyring() else "file"


def _file_for(ref: str):
    return SECRETS_DIR / _SAFE_REF.sub("_", ref)


def get_secret(ref: str) -> str | None:
    kr = _keyring()
    if kr:
        try:
            value = kr.get_password(*_split_ref(ref))
            if value:
                return value
        except Exception as e:
            log.warning("Keyring read failed for %s: %s", ref, e)
    path = _file_for(ref)
    if path.is_file():
        try:
            return path.read_text().strip() or None
        except OSError:
            return None
    return None


def set_secret(ref: str, value: str) -> str:
    """Store a secret. Returns the backend that took it ('keyring' | 'file')."""
    kr = _keyring()
    if kr:
        try:
            kr.set_password(*_split_ref(ref), value)
            # Drop a stale file copy so there is one source of truth.
            _file_for(ref).unlink(missing_ok=True)
            return "keyring"
        except Exception as e:
            log.warning("Keyring write failed for %s, using file store: %s", ref, e)
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        SECRETS_DIR.chmod(0o700)
    path = _file_for(ref)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value)
    return "file"


def delete_secret(ref: str) -> bool:
    removed = False
    kr = _keyring()
    if kr:
        try:
            if kr.get_password(*_split_ref(ref)) is not None:
                kr.delete_password(*_split_ref(ref))
                removed = True
        except Exception as e:
            log.warning("Keyring delete failed for %s: %s", ref, e)
    path = _file_for(ref)
    if path.is_file():
        path.unlink()
        removed = True
    return removed


def has_secret(ref: str) -> bool:
    return get_secret(ref) is not None
