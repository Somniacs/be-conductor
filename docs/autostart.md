# Auto-Start on Boot

Set up Be-Conductor to start automatically when your machine boots, so the dashboard is always reachable.

> **Tip:** The installer (`install.sh` / `install.ps1`) offers to configure autostart for you during installation. The manual steps below are only needed if you skipped that prompt or want to customize the configuration.

## Linux (systemd)

Create a user service:

```bash
mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/be-conductor.service << 'EOF'
[Unit]
Description=Be-Conductor Server
After=network.target

[Service]
ExecStart=%h/.local/bin/be-conductor serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
```

Enable and start:

```bash
systemctl --user daemon-reload
systemctl --user enable be-conductor
systemctl --user start be-conductor
```

To survive logouts (run once):

```bash
loginctl enable-linger $USER
```

Check status:

```bash
systemctl --user status be-conductor
```

View logs:

```bash
journalctl --user -u be-conductor -f
```

> **Note:** If you installed Be-Conductor to a different path, adjust the `ExecStart` line. Find it with `which be-conductor`.

## Linux (cron @reboot)

For systems without systemd (Alpine, Void, WSL, etc.), use cron as a lightweight alternative. The installer uses this automatically when systemd is not available.

```bash
crontab -e
```

Add this line:

```
@reboot /home/YOUR_USER/.local/bin/be-conductor serve >> /tmp/be-conductor.log 2>&1
```

Replace `/home/YOUR_USER/.local/bin/be-conductor` with the output of `which be-conductor`.

To remove:

```bash
crontab -l | grep -v 'be-conductor serve' | crontab -
```

> **Note:** cron `@reboot` does not restart the server if it crashes. For automatic restart, use systemd or a process supervisor.

## Linux (OpenRC)

For Gentoo, Alpine, or Artix with OpenRC. Requires root.

```bash
sudo tee /etc/init.d/be-conductor << 'EOF'
#!/sbin/openrc-run

name="Be-Conductor Server"
description="Be-Conductor terminal session orchestrator"
command="/home/YOUR_USER/.local/bin/be-conductor"
command_args="serve"
command_user="YOUR_USER"
command_background=true
pidfile="/run/be-conductor.pid"
output_log="/var/log/be-conductor.log"
error_log="/var/log/be-conductor.err"

depend() {
    need net
}
EOF

sudo chmod +x /etc/init.d/be-conductor
sudo rc-update add be-conductor default
sudo rc-service be-conductor start
```

Replace `YOUR_USER` with your username. To remove:

```bash
sudo rc-service be-conductor stop
sudo rc-update del be-conductor default
sudo rm /etc/init.d/be-conductor
```

## Linux (runit)

For Void Linux or other runit-based systems. Requires root.

```bash
sudo mkdir -p /etc/sv/be-conductor
sudo tee /etc/sv/be-conductor/run << 'EOF'
#!/bin/sh
exec chpst -u YOUR_USER /home/YOUR_USER/.local/bin/be-conductor serve
EOF

sudo chmod +x /etc/sv/be-conductor/run
sudo ln -s /etc/sv/be-conductor /var/service/
```

Replace `YOUR_USER` with your username. To remove:

```bash
sudo rm /var/service/be-conductor
sudo rm -rf /etc/sv/be-conductor
```

## macOS (launchd)

Create a LaunchAgent:

```bash
cat > ~/Library/LaunchAgents/com.be-conductor.server.plist << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.be-conductor.server</string>
    <key>ProgramArguments</key>
    <array>
        <string>$(which be-conductor)</string>
        <string>serve</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/be-conductor.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/be-conductor.err</string>
</dict>
</plist>
EOF
```

> **Important:** Replace `$(which be-conductor)` with the actual path — run `which be-conductor` and paste the full path into the plist.

Load it:

```bash
launchctl load ~/Library/LaunchAgents/com.be-conductor.server.plist
```

To stop and unload:

```bash
launchctl unload ~/Library/LaunchAgents/com.be-conductor.server.plist
```

## Windows (Task Scheduler)

Open PowerShell as your user:

```powershell
$conductorPath = (Get-Command be-conductor).Source

$action = New-ScheduledTaskAction -Execute $conductorPath -Argument "serve"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName "Be-Conductor" -Action $action -Trigger $trigger -Settings $settings -Description "Be-Conductor Server"
```

To remove:

```powershell
Unregister-ScheduledTask -TaskName "Be-Conductor" -Confirm:$false
```

Alternatively, place a shortcut to `be-conductor serve` in your Startup folder:

```
Win+R → shell:startup → create shortcut → be-conductor serve
```

## Verify

After reboot, check that Be-Conductor is running:

```bash
be-conductor status
```

Or open the dashboard in your browser at `http://127.0.0.1:7777`.
