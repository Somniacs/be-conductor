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

"""Account profiles — isolated agent logins applied to a session's environment."""

from be_conductor.profiles.manager import (
    BACKEND_CLI,
    BACKEND_ENV,
    DEFAULT_STRIP_ENV,
    ProfileError,
    SessionEnv,
    build_session_env,
    get_profile,
    list_profiles,
    profile_status,
    save_profiles,
    validate_profile,
)

__all__ = [
    "BACKEND_CLI",
    "BACKEND_ENV",
    "DEFAULT_STRIP_ENV",
    "ProfileError",
    "SessionEnv",
    "build_session_env",
    "get_profile",
    "list_profiles",
    "profile_status",
    "save_profiles",
    "validate_profile",
]
