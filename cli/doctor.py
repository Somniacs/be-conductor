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

"""`be-conductor doctor` and `be-conductor setup-acp`.

`doctor` is a read-only environment diagnostic — Python, Node, npx, git,
the agent CLIs, and the ACP adapters. `setup-acp` is an interactive
installer that asks which ACP agents the user wants, warms the npx
cache for them so the first session is instant, and persists the
choice to ~/.be-conductor/config.yaml.

Both are cross-platform: dependency detection uses `shutil.which`
(which resolves `.cmd`/`.exe` on Windows), and the npm cache is warmed
via the resolved `npx` path so it works on Windows too. Neither command
installs Node.js — they detect it and print the OS-appropriate fix.
"""

import json
import shutil
import subprocess
import sys

import click

# Marks for the table — kept ASCII-safe so Windows consoles render them.
_OK = "OK "
_NO = "MISSING"
_WARN = "WARN"


def _python_version() -> tuple[int, int, int]:
    return sys.version_info[:3]


def _which(name: str) -> str | None:
    return shutil.which(name)


def _collect_status() -> dict:
    """Gather the full environment picture as a plain dict.

    Shared by `doctor` (renders a table) and `doctor --json`.
    """
    from be_conductor.sessions.providers import acp

    py = _python_version()
    node = acp.find_node()
    node_ver = acp.node_version()
    npx = acp.acp_npx()

    agents = []
    for key, meta in acp.ACP_AGENTS.items():
        agents.append({
            "key": key,
            "label": meta["label"],
            "npm": meta["npm"],
            "cli": meta["cli"],
            "cli_found": _which(meta["cli"]) is not None,
        })

    return {
        "python": {
            "version": ".".join(str(p) for p in py),
            "ok": py >= (3, 10),
        },
        "node": {
            "found": node is not None,
            "path": node,
            "version": (".".join(str(p) for p in node_ver) if node_ver else None),
            "ok": node_ver is not None and node_ver[0] >= acp.ACP_MIN_NODE_MAJOR,
            "min_major": acp.ACP_MIN_NODE_MAJOR,
        },
        "npx": {"found": npx is not None, "path": npx},
        "git": {"found": _which("git") is not None},
        "claude_cli": {"found": _which("claude") is not None},
        "opencode": {"found": _which("opencode") is not None},
        "acp_ready": acp.acp_preflight() is None,
        "acp_preflight_error": acp.acp_preflight(),
        "acp_agents": agents,
    }


def _node_install_hint() -> str:
    if sys.platform == "win32":
        return "winget install OpenJS.NodeJS   (or: choco install nodejs)"
    if sys.platform == "darwin":
        return "brew install node"
    return "install Node.js 20+ from https://nodejs.org/ or your package manager"


def _render_doctor(st: dict) -> None:
    """Pretty-print the status dict as a human-readable report."""
    def line(mark: str, label: str, detail: str = "") -> None:
        click.echo(f"  [{mark:^7}] {label}" + (f"  {detail}" if detail else ""))

    click.echo("be-conductor doctor\n")

    # Core
    py = st["python"]
    line(_OK if py["ok"] else _NO, "Python", py["version"]
         + ("" if py["ok"] else "  — needs 3.10+"))

    git = st["git"]
    line(_OK if git["found"] else _WARN, "git",
         "" if git["found"] else "— needed for the worktree feature")

    click.echo("\nAgent backends:")
    cl = st["claude_cli"]
    line(_OK if cl["found"] else _WARN, "claude CLI",
         "native Claude sessions" if cl["found"]
         else "— sign in to Claude Code to use native Claude")
    oc = st["opencode"]
    line(_OK if oc["found"] else _WARN, "opencode",
         "OpenCode sessions" if oc["found"]
         else "— optional; install from https://opencode.ai")

    # ACP block
    click.echo("\nACP agents (Agent Client Protocol):")
    node = st["node"]
    if node["ok"]:
        line(_OK, "Node.js", node["version"])
    elif node["found"]:
        line(_NO, "Node.js",
             f"{node['version']} — too old, need {node['min_major']}+. "
             + _node_install_hint())
    else:
        line(_NO, "Node.js", "not found. " + _node_install_hint())
    npx = st["npx"]
    line(_OK if npx["found"] else _NO, "npx",
         "" if npx["found"] else "— ships with Node.js")

    for a in st["acp_agents"]:
        if a["cli_found"]:
            detail = f"{a['cli']} CLI signed in"
        else:
            detail = f"— {a['cli']} CLI not found; sign in to use this agent"
        mark = _OK if (st["acp_ready"] and a["cli_found"]) else _WARN
        line(mark, a["label"], detail)

    click.echo("")
    if st["acp_ready"]:
        click.echo("ACP is ready. Run `be-conductor setup-acp` to pre-install "
                   "adapters so the first session starts instantly.")
    else:
        click.echo("ACP is not ready: " + (st["acp_preflight_error"] or ""))
        click.echo("Fix the above, then run: be-conductor setup-acp")


def doctor(use_json: bool) -> None:
    """Check be-conductor's environment and dependencies."""
    st = _collect_status()
    if use_json:
        click.echo(json.dumps(st, indent=2))
        return
    _render_doctor(st)
    # Exit non-zero only when something *core* is wrong (Python). A
    # missing optional agent backend is a warning, not a failure.
    if not st["python"]["ok"]:
        sys.exit(1)


def setup_acp(agents_opt: str | None, assume_yes: bool) -> None:
    """Interactively install ACP agents (Claude / Codex / Gemini).

    Warms the npm cache for the chosen adapters so the first session
    starts without a download pause, and records the choice in
    ~/.be-conductor/config.yaml.
    """
    from be_conductor.sessions.providers import acp
    from be_conductor.utils import config as cfg

    click.echo("be-conductor — ACP agent setup\n")

    # 1) Preflight — Node.js must be present and recent enough.
    problem = acp.acp_preflight()
    if problem:
        click.echo(problem, err=True)
        click.echo("\nInstall a recent Node.js, then run "
                   "`be-conductor setup-acp` again.", err=True)
        sys.exit(1)
    ver = acp.node_version()
    click.echo(f"Node.js {'.'.join(str(p) for p in ver)}  OK")
    click.echo("")

    all_keys = list(acp.ACP_AGENTS.keys())

    # 2) Decide which agents to enable.
    if agents_opt:
        chosen = [k.strip() for k in agents_opt.split(",") if k.strip()]
        unknown = [k for k in chosen if k not in acp.ACP_AGENTS]
        if unknown:
            click.echo(f"Unknown ACP agent(s): {', '.join(unknown)}. "
                       f"Valid: {', '.join(all_keys)}", err=True)
            sys.exit(1)
    else:
        chosen = []
        click.echo("Which ACP agents do you want to enable?")
        for key in all_keys:
            meta = acp.ACP_AGENTS[key]
            cli_ok = acp.agent_cli_status(key)
            status = (f"{meta['cli']} CLI detected"
                      if cli_ok else f"{meta['cli']} CLI not found")
            # Default-yes only when the underlying CLI is present.
            default = cli_ok if not assume_yes else True
            if assume_yes:
                pick = default
            else:
                pick = click.confirm(f"  {meta['label']}  ({status})",
                                     default=default)
            if pick:
                chosen.append(key)

    if not chosen:
        click.echo("\nNo agents selected — nothing to do.")
        return

    # 3) Warm the npx cache for each chosen agent.
    click.echo(f"\nInstalling {len(chosen)} ACP adapter(s) "
               "(first download can take a minute)…\n")
    npx = acp.acp_npx()
    installed: list[str] = []
    failed: list[str] = []
    for key in chosen:
        meta = acp.ACP_AGENTS[key]
        pkg = meta["npm"]
        click.echo(f"  {meta['label']}  —  {pkg}")
        # `npx -y <pkg> --version` downloads + caches the package
        # without launching the adapter for real.
        cmd = [npx, "-y", pkg, "--version"]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=300)
            if res.returncode == 0:
                click.echo("      cached OK")
                installed.append(key)
            else:
                # Some adapters don't implement --version; a non-zero
                # exit after a successful download is still a warm
                # cache. Treat "downloaded" as success unless npx
                # itself failed to fetch.
                err = (res.stderr or "").lower()
                if "npm error" in err or "enotfound" in err or \
                        "404" in err or "enetunreach" in err:
                    click.echo(f"      FAILED — {res.stderr.strip()[:200]}",
                               err=True)
                    failed.append(key)
                else:
                    click.echo("      cached OK")
                    installed.append(key)
        except subprocess.TimeoutExpired:
            click.echo("      FAILED — timed out after 300s", err=True)
            failed.append(key)
        except Exception as e:
            click.echo(f"      FAILED — {e}", err=True)
            failed.append(key)

    # 4) Persist the choice.
    if installed:
        try:
            cfg.set_acp_agents(installed)
        except Exception as e:
            click.echo(f"\nWarning: could not save preference: {e}", err=True)

    # 5) Summary.
    click.echo("")
    if installed:
        labels = ", ".join(acp.ACP_AGENTS[k]["label"] for k in installed)
        click.echo(f"Ready: {labels}")
        click.echo("Open the dashboard, start a new Agent session, and pick "
                   "one of the ACP entries in the Agent picker.")
    if failed:
        labels = ", ".join(acp.ACP_AGENTS[k]["label"] for k in failed)
        click.echo(f"Failed: {labels} — check your network / npm and retry.",
                   err=True)
        sys.exit(1)


def register(cli) -> None:
    """Attach the `doctor` and `setup-acp` commands to the Click group.

    Called once from cli/main.py. Kept as a function to avoid importing
    the `cli` group at module load time (circular import).
    """
    cli.command(name="doctor")(
        click.option("--json", "use_json", is_flag=True,
                     help="Output the report as JSON")(doctor)
    )
    cli.command(name="setup-acp")(
        click.option("--agents", "agents_opt", default=None,
                     help="Comma-separated agent keys "
                          "(claude,codex,gemini) for non-interactive use")(
            click.option("--yes", "-y", "assume_yes", is_flag=True,
                         help="Skip prompts; accept defaults")(setup_acp)
        )
    )
