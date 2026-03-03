# be-conductor JetBrains Plugin

Toolbar button for CLion, IntelliJ IDEA, PyCharm, WebStorm, GoLand, and all other JetBrains IDEs. Opens a dialog to pick an AI agent and name a session, then runs `be-conductor run <agent> <name>` in a new terminal tab.

## Features

- **Agent picker** — dropdown with all supported agents (Claude, Codex, Aider, Gemini, Copilot, OpenCode, Amp, Goose, Forge, Cursor)
- **Session name input** — validated to letters, digits, hyphens, and underscores
- **Terminal tab** — opens in the IDE's built-in Terminal tool window with the session name as tab title

## Requirements

- Java 17+
- Any JetBrains IDE 2024.1 or later (CLion, IntelliJ IDEA, PyCharm, WebStorm, GoLand, Rider, etc.)
- The built-in Terminal plugin (enabled by default in all JetBrains IDEs)
- `be-conductor` installed and in PATH

## Install

### From zip

1. Build the plugin (see below) or download `be-conductor-plugin-0.1.0.zip` from a release
2. In your IDE: **Settings → Plugins → gear icon → Install Plugin from Disk**
3. Select the zip file and restart the IDE

### Build from source

```bash
cd tools/jetbrains/be-conductor-plugin
./gradlew buildPlugin
```

Output: `build/distributions/be-conductor-plugin-0.1.0.zip`

### Development

```bash
# Launch a sandboxed CLion instance with the plugin loaded
./gradlew runIde

# Validate plugin.xml
./gradlew verifyPlugin
```

## Usage

1. Click the **♭** button in the main toolbar (or **Tools → New be-conductor Session**)
2. Select an AI agent from the dropdown
3. Enter a session name
4. Click **OK** — a new terminal tab opens and runs the session

## Project structure

```
be-conductor-plugin/
├── build.gradle                         # Groovy DSL
├── settings.gradle
├── gradle.properties
├── gradlew / gradlew.bat
├── gradle/wrapper/
└── src/main/
    ├── java/com/somniacs/beconductor/
    │   ├── RunSessionAction.java        # Toolbar action
    │   └── NewSessionDialog.java        # Agent picker + name input dialog
    └── resources/
        ├── META-INF/plugin.xml
        └── icons/be-conductor.svg
```
