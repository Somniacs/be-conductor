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

"""CLI: account profiles, headless tasks and the MCP bridge."""

import json
import os
import sys
import time

import click
import httpx


def register(cli):
    from cli import main as m

    def _ensure_server(quiet: bool = False):
        if m.server_running():
            return
        if not quiet:
            click.echo("Server not running. Starting daemon...", err=True)
        if not m.start_server_daemon():
            click.echo("Failed to start server. Try: be-conductor serve", err=True)
            sys.exit(1)

    def _api(method: str, path: str, quiet: bool = False, timeout: float = 15, **kw):
        _ensure_server(quiet)
        r = httpx.request(method, f"{m.get_base_url()}{path}", headers=m._auth_headers(),
                          timeout=timeout, **m._http_kwargs(), **kw)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            click.echo(f"Error: {detail}", err=True)
            sys.exit(1)
        return r.json()

    def _raw_profiles() -> list[dict]:
        # Read through the server so CLI and daemon never disagree on config.
        keep = ("name", "backend", "config_dir", "allow_default_dir", "env", "secrets",
                "strip_env", "leanctx", "max_cost_usd_per_run", "model", "description")
        out = []
        for p in _api("GET", "/profiles")["profiles"]:
            clean = {k: p[k] for k in keep if k in p}
            clean["secrets"] = [{"env": s["env"], "keyring": s["keyring"]}
                                for s in clean.get("secrets", [])]
            out.append(clean)
        return out

    # ── profile group ─────────────────────────────────────────────────────

    @cli.group("profile")
    def profile_group():
        """Account profiles — run agents under a separate login or API key."""

    @profile_group.command("list")
    @click.option("--json", "use_json", is_flag=True, help="Output raw JSON")
    def profile_list(use_json):
        """List profiles and their status."""
        data = _api("GET", "/profiles")
        if use_json:
            click.echo(json.dumps(data, indent=2))
            return
        if not data["profiles"]:
            click.echo("No profiles. Add one: be-conductor profile add NAME --backend claude")
            return
        for p in data["profiles"]:
            if p["secrets"]:
                auth = ", ".join(f"{s['env']}={'set' if s['set'] else 'MISSING'}" for s in p["secrets"])
            elif p.get("logged_in") is None:
                auth = "login: unknown"
            else:
                auth = "logged in" if p["logged_in"] else "not logged in"
            click.echo(f"{p['name']:<20} {p['backend']:<9} {auth:<28} "
                       f"today ${p['usage_today_usd']:.2f}  week ${p['usage_week_usd']:.2f}")
            click.echo(f"{'':<20} {p.get('config_dir', '-')}"
                       + ("" if p.get("cli_found") is not False else "   (CLI not found on PATH)"))
        if data.get("secret_backend") == "file":
            click.echo("\nNote: no OS keyring available — secrets are stored in "
                       "~/.be-conductor/secrets/ (mode 0600).", err=True)

    @profile_group.command("add")
    @click.argument("name")
    @click.option("--backend", type=click.Choice(["claude", "codex", "opencode", "custom"]),
                  required=True)
    @click.option("--config-dir", default=None, help="Default: ~/.be-conductor/profiles/NAME")
    @click.option("--model", default=None, help="Model for headless runs ({model} placeholder)")
    @click.option("--secret", "secret_envs", multiple=True, metavar="ENV",
                  help="Env var to inject from the keyring (repeatable); set it with set-key")
    @click.option("--max-cost", type=float, default=None, help="USD cap per headless run")
    @click.option("--description", default=None)
    def profile_add(name, backend, config_dir, model, secret_envs, max_cost, description):
        """Add a profile."""
        profiles = _raw_profiles()
        if any(p["name"] == name for p in profiles):
            click.echo(f"Profile '{name}' already exists (use `profile edit`).", err=True)
            sys.exit(1)
        new = {"name": name, "backend": backend}
        if config_dir:
            new["config_dir"] = config_dir
        if model:
            new["model"] = model
        if secret_envs:
            new["secrets"] = [{"env": e} for e in secret_envs]
        if max_cost:
            new["max_cost_usd_per_run"] = max_cost
        if description:
            new["description"] = description
        _api("PUT", "/admin/profiles", json={"profiles": profiles + [new]})
        click.echo(f"Profile '{name}' added.")
        if secret_envs:
            for e in secret_envs:
                click.echo(f"  next: be-conductor profile set-key {name} {e}")
        elif backend != "custom":
            click.echo(f"  next: be-conductor profile login {name}")

    @profile_group.command("edit")
    @click.argument("name")
    @click.option("--config-dir", default=None)
    @click.option("--model", default=None)
    @click.option("--max-cost", type=float, default=None, help="USD cap per run (0 = no cap)")
    @click.option("--description", default=None)
    @click.option("--set-env", "set_env", multiple=True, metavar="KEY=VALUE")
    @click.option("--unset-env", "unset_env", multiple=True, metavar="KEY")
    def profile_edit(name, config_dir, model, max_cost, description, set_env, unset_env):
        """Change fields of a profile."""
        profiles = _raw_profiles()
        target = next((p for p in profiles if p["name"] == name), None)
        if target is None:
            click.echo(f"Unknown profile '{name}'.", err=True)
            sys.exit(1)
        if config_dir is not None:
            target["config_dir"] = config_dir
        if model is not None:
            target["model"] = model
        if max_cost is not None:
            target["max_cost_usd_per_run"] = max_cost or None
        if description is not None:
            target["description"] = description
        env = dict(target.get("env") or {})
        for item in set_env:
            key, sep, value = item.partition("=")
            if not sep:
                click.echo(f"--set-env expects KEY=VALUE, got {item!r}", err=True)
                sys.exit(1)
            env[key] = value
        for key in unset_env:
            env.pop(key, None)
        target["env"] = env
        _api("PUT", "/admin/profiles", json={"profiles": profiles})
        click.echo(f"Profile '{name}' updated.")

    @profile_group.command("remove")
    @click.argument("name")
    @click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
    def profile_remove(name, yes):
        """Remove a profile from config. Its login directory is left on disk."""
        profiles = _raw_profiles()
        target = next((p for p in profiles if p["name"] == name), None)
        if target is None:
            click.echo(f"Unknown profile '{name}'.", err=True)
            sys.exit(1)
        if not yes:
            click.confirm(f"Remove profile '{name}'?", abort=True)
        for s in target.get("secrets", []):
            _api("DELETE", f"/admin/profiles/{name}/secret/{s['env']}")
        _api("PUT", "/admin/profiles",
             json={"profiles": [p for p in profiles if p["name"] != name]})
        click.echo(f"Profile '{name}' removed.")
        if target.get("config_dir"):
            click.echo(f"Its login data is still in {target['config_dir']} — delete it by hand if unwanted.")

    @profile_group.command("login")
    @click.argument("name")
    def profile_login(name):
        """Log the profile in — opens the agent's login flow and attaches."""
        import shutil
        size = shutil.get_terminal_size()
        data = _api("POST", f"/profiles/{name}/login",
                    json={"rows": size.lines, "cols": size.columns})
        click.echo(f"Login session '{data['name']}' started — also visible in the dashboard. "
                   "(Ctrl+] to detach)")
        m._resize_session(data["name"])
        m._attach_session(data["name"], stop_on_exit=True)

    @profile_group.command("check")
    @click.argument("name")
    def profile_check(name):
        """Run a trivial prompt under the profile to prove it works."""
        click.echo(f"Checking '{name}' (up to 60 s)...")
        r = _api("POST", f"/profiles/{name}/check", timeout=100)
        if r.get("ok"):
            cost = f", ${r['cost_usd']:.4f}" if r.get("cost_usd") is not None else ""
            click.echo(f"OK — answered in {r.get('duration_s', 0):.0f}s{cost}: {r.get('result', '').strip()[:80]}")
        else:
            click.echo(f"FAILED — {r.get('error') or r.get('fail_reason') or r.get('task_status')}", err=True)
            if r.get("result"):
                click.echo(r["result"], err=True)
            sys.exit(1)

    @profile_group.command("set-key")
    @click.argument("name")
    @click.argument("env")
    def profile_set_key(name, env):
        """Store a secret (e.g. an API key) for the profile in the OS keyring."""
        value = click.prompt(f"{env} for profile '{name}'", hide_input=True)
        r = _api("POST", f"/admin/profiles/{name}/secret", json={"env": env, "value": value})
        where = "OS keyring" if r["backend"] == "keyring" else "~/.be-conductor/secrets/ (no keyring available)"
        click.echo(f"Stored in the {where}.")

    @profile_group.command("purge")
    @click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
    def profile_purge(yes):
        """Delete ALL profiles: stored API keys, login directories and config.

        Used by the uninstaller. Works without the server.
        """
        import shutil
        import be_conductor.utils.config as cfg
        from be_conductor.profiles import list_profiles, secrets
        if not yes:
            click.confirm(f"Delete every profile, its logins under {cfg.PROFILES_DIR} "
                          "and its stored API keys?", abort=True)
        n = 0
        for p in list_profiles():
            for sec in p["secrets"]:
                if secrets.delete_secret(sec["keyring"]):
                    n += 1
        shutil.rmtree(cfg.PROFILES_DIR, ignore_errors=True)
        shutil.rmtree(cfg.SECRETS_DIR, ignore_errors=True)
        if cfg.PROFILES:
            cfg.PROFILES = []
            cfg.save_user_config({})
        click.echo(f"Profiles removed ({n} stored key(s) deleted).")

    @profile_group.command("leanctx")
    @click.argument("name")
    @click.argument("mode", type=click.Choice(["off", "shadow", "active"]))
    def profile_leanctx(name, mode):
        """Set up (or remove) LeanCTX context compression for one profile.

        Only ever touches the profile's own config dir.
        """
        r = _api("POST", f"/admin/profiles/{name}/leanctx", json={"mode": mode}, timeout=150)
        click.echo(r.get("message", ""), err=not r.get("ok"))
        if not r.get("ok"):
            sys.exit(1)

    @profile_group.command("usage")
    @click.argument("name")
    @click.option("--json", "use_json", is_flag=True)
    def profile_usage(name, use_json):
        """Cost and token totals from the profile's ledger."""
        u = _api("GET", f"/profiles/{name}/usage")
        if use_json:
            click.echo(json.dumps(u, indent=2))
            return
        for key, title in (("today", "Today"), ("week", "Last 7 days"), ("total", "All time")):
            b = u[key]
            click.echo(f"{title:<12} {b['runs']:>4} runs   ${b['cost_usd']:>8.4f}   {b['tokens']:>10,} tokens")
        for d in u["daily"][-7:]:
            click.echo(f"  {d['date']}  {d['runs']:>4} runs   ${d['cost_usd']:>8.4f}")

    # ── task ──────────────────────────────────────────────────────────────

    @cli.command("task")
    @click.argument("command")
    @click.argument("prompt")
    @click.option("--profile", "-p", default=None, help="Account profile to run under")
    @click.option("--model", "-m", default=None, help="Model for this run (default: the profile's, else the agent's own)")
    @click.option("--dir", "-C", "workdir", default=None, help="Working directory (default: cwd)")
    @click.option("-w", "--worktree", is_flag=True, help="Run in an isolated git worktree")
    @click.option("--timeout", type=float, default=None, help="Seconds before the run is killed")
    @click.option("-d", "--detach", is_flag=True, help="Start it and print the session name")
    @click.option("--json", "use_json", is_flag=True, help="Output the full task record as JSON")
    def task(command, prompt, profile, model, workdir, worktree, timeout, detach, use_json):
        """Run a prompt headless and print the agent's answer.

        COMMAND is a label or command from allowed_commands.

        \b
        Examples:
            be-conductor task claude "summarize README.md"
            be-conductor task "Claude Code — Max5" "fix the failing test" -w
            be-conductor task claude "plan the refactor" --model claude-fable-5-1
            be-conductor task opencode "review src/" --profile openrouter-any --json
        """
        from be_conductor.sessions.tasks import format_footer, task_name
        payload = {
            "name": task_name(command), "command": command, "headless": True,
            "prompt": prompt, "cwd": os.path.abspath(workdir or os.getcwd()),
            "source": "cli", "worktree": worktree,
        }
        if profile:
            payload["profile"] = profile
        if model:
            payload["model"] = model
        if timeout:
            payload["timeout_seconds"] = timeout
        data = _api("POST", "/sessions/run", quiet=use_json, json=payload)
        sid = data["id"]
        if detach:
            click.echo(json.dumps(data, indent=2) if use_json else sid)
            return
        try:
            notified = False
            while True:
                rec = _api("GET", f"/sessions/{sid}/result", quiet=True)
                status = rec.get("task_status")
                if status not in ("running", "needs_input"):
                    break
                if status == "needs_input" and not notified and not use_json:
                    click.echo(f"Waiting for input — answer it with: be-conductor attach {sid}", err=True)
                    notified = True
                time.sleep(1.0)
        except KeyboardInterrupt:
            click.echo(f"\nDetached — the run continues as session '{sid}'.", err=True)
            sys.exit(130)
        if use_json:
            click.echo(json.dumps(rec, indent=2))
        else:
            click.echo(rec.get("result") or "")
            click.echo(format_footer(rec), err=True)
        sys.exit(0 if rec.get("task_status") == "done" else 1)

    # ── MCP ───────────────────────────────────────────────────────────────

    @cli.command("mcp")
    @click.option("--server", default=None, metavar="URL",
                  help="Forward to a remote be-conductor instead of the local one")
    @click.option("--token", default=None, help="Bearer token for --server (or BE_CONDUCTOR_TOKEN)")
    def mcp_bridge(server, token):
        """stdio MCP server for Claude Desktop — bridges to the server's /mcp."""
        from cli.mcp_bridge import run_bridge
        import be_conductor.utils.config as cfg
        if server:
            run_bridge(server, token or os.environ.get("BE_CONDUCTOR_TOKEN"),
                       local=False, verify=False)
        else:
            run_bridge(m.get_base_url(), token or cfg.CONDUCTOR_TOKEN,
                       local=True, verify=not cfg.SSL_CERTFILE)

    _target_opt = click.option(
        "--target", type=click.Choice(["desktop", "code", "both"]), default="both",
        help="desktop = Claude Desktop chat, code = Claude Code (CLI + the Code tab)")

    def _report(results: dict, verb: str) -> bool:
        from be_conductor.mcp_server.install import LABELS
        ok = True
        for target, r in results.items():
            label = LABELS[target]
            if r.get("error"):
                click.echo(f"  {label}: FAILED — {r['error']}", err=True)
                ok = False
            elif r.get("skipped"):
                click.echo(f"  {label}: skipped ({r['skipped']})")
            elif verb == "install":
                state = "registered" if r.get("changed", True) else "already registered"
                click.echo(f"  {label}: {state} in {r['config_path']}"
                           + (f"  (backup: {r['backup']})" if r.get("backup") else ""))
            else:
                click.echo(f"  {label}: " + ("removed" if r.get("removed") else "was not registered"))
        return ok

    @cli.command("install-mcp")
    @_target_opt
    def install_mcp(target):
        """Register be-conductor as an MCP server in Claude Desktop and Claude Code."""
        from be_conductor.mcp_server import install
        import be_conductor.utils.config as cfg
        try:
            results = install.install(target)
        except Exception as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        ok = _report(results, "install")
        click.echo(f"  Command: {install.bridge_command()} mcp")
        if not cfg.MCP_CONFIG.get("enabled"):
            click.echo("\nMCP is not enabled yet — set `mcp: {enabled: true, allowed_dirs: [...]}` in "
                       "~/.be-conductor/config.yaml or use Settings → MCP in the dashboard.")
        click.echo("Restart Claude Desktop / start a new Claude Code session to pick it up.")
        if not ok:
            sys.exit(1)

    @cli.command("uninstall-mcp")
    @_target_opt
    def uninstall_mcp(target):
        """Remove be-conductor from Claude Desktop's and Claude Code's MCP servers."""
        from be_conductor.mcp_server import install
        try:
            results = install.uninstall(target)
        except Exception as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        if not _report(results, "uninstall"):
            sys.exit(1)
