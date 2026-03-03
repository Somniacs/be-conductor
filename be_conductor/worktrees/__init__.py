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

"""Git worktree management — isolated filesystem per session."""

from be_conductor.worktrees.manager import MergeResult, WorktreeInfo, WorktreeManager

__all__ = ["WorktreeInfo", "WorktreeManager", "MergeResult"]
