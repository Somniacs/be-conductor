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

"""Per-profile cost ledger — one JSON line per finished headless run."""

import json
import logging
import time
from datetime import datetime, timedelta

from be_conductor.utils.config import PROFILES_DIR

log = logging.getLogger(__name__)


def _ledger_path(profile: str):
    return PROFILES_DIR / profile / "ledger.jsonl"


def _check_path(profile: str):
    return PROFILES_DIR / profile / "last_check.json"


def record(profile: str, session: str, cost_usd: float | None,
           tokens: dict | None = None, status: str | None = None,
           duration_s: float | None = None):
    path = _ledger_path(profile)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": time.time(), "session": session, "cost_usd": cost_usd,
                 "tokens": tokens or None, "status": status,
                 "duration_s": round(duration_s, 1) if duration_s else None}
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        log.warning("Could not write ledger for profile %s: %s", profile, e)


def _entries(profile: str):
    path = _ledger_path(profile)
    if not path.is_file():
        return
    with open(path) as f:
        for line in f:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def usage(profile: str, days: int = 14) -> dict:
    """Totals for today / the last 7 days, plus a per-day breakdown."""
    now = datetime.now()
    today = now.date()
    week_start = today - timedelta(days=6)
    first_day = today - timedelta(days=days - 1)

    def bucket():
        return {"runs": 0, "cost_usd": 0.0, "tokens": 0}

    out = {"today": bucket(), "week": bucket(), "total": bucket()}
    daily: dict[str, dict] = {}

    for e in _entries(profile) or ():
        day = datetime.fromtimestamp(e.get("ts", 0)).date()
        cost = e.get("cost_usd") or 0.0
        tok = sum(v for v in (e.get("tokens") or {}).values()
                  if isinstance(v, (int, float)))
        targets = [out["total"]]
        if day == today:
            targets.append(out["today"])
        if day >= week_start:
            targets.append(out["week"])
        if day >= first_day:
            targets.append(daily.setdefault(day.isoformat(), bucket()))
        for b in targets:
            b["runs"] += 1
            b["cost_usd"] += cost
            b["tokens"] += tok

    for b in (*out.values(), *daily.values()):
        b["cost_usd"] = round(b["cost_usd"], 4)
    out["daily"] = [{"date": d, **daily[d]} for d in sorted(daily)]
    return out


def record_check(profile: str, ok: bool, detail: str = ""):
    path = _check_path(profile)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"ts": time.time(), "ok": ok, "detail": detail[:500]}))
    except OSError:
        pass


def last_check(profile: str) -> dict | None:
    try:
        return json.loads(_check_path(profile).read_text())
    except (OSError, ValueError):
        return None
