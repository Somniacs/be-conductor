package com.somniacs.beconductor;

import com.intellij.openapi.fileChooser.FileChooserDescriptorFactory;
import com.intellij.openapi.project.Project;
import com.intellij.openapi.ui.DialogWrapper;
import com.intellij.openapi.ui.TextFieldWithBrowseButton;
import com.intellij.openapi.ui.ValidationInfo;
import com.intellij.ui.components.JBCheckBox;
import com.intellij.ui.components.JBLabel;
import com.intellij.ui.components.JBTextField;
import com.somniacs.beconductor.api.ApiModels;
import com.somniacs.beconductor.api.BeConductorClient;
import com.somniacs.beconductor.api.ServerRegistry;
import org.jetbrains.annotations.Nullable;

import javax.swing.*;
import javax.swing.event.DocumentEvent;
import javax.swing.event.DocumentListener;
import java.awt.*;
import java.util.List;

public class NewSessionDialog extends DialogWrapper {

    private static final String[] FALLBACK_AGENTS = {
        "claude", "codex", "aider", "gemini", "copilot",
        "opencode", "amp", "goose", "forge", "cursor"
    };

    private static final String DEFAULT_AGENT = "claude";

    private JComboBox<String> serverCombo;
    private JBTextField nameField;
    private JComboBox<String> commandCombo;
    private TextFieldWithBrowseButton cwdField;
    private JBCheckBox worktreeCheckbox;
    private JComboBox<String> sessionTypeCombo;
    /** Agent picker (GUI mode only) — Claude (native) plus one entry per OpenCode model. */
    private JComboBox<AgentChoice> agentCombo;
    private JBLabel agentLabel;
    private JBLabel agentStatus;
    private JBLabel branchPreview;
    private JBLabel gitStatus;

    private List<ApiModels.CommandConfig> serverCommands;
    private boolean isGitRepo = false;
    private String currentBranch = "";
    private String selectedServerKey = "local";
    private List<ServerRegistry.Server> enabledServers;

    /**
     * Entry in the GUI-mode agent combo box. Either Claude (native)
     * or an OpenCode model. We send agent_options accordingly.
     */
    private static class AgentChoice {
        final String label;
        final String provider;     // null for Claude (native), "opencode" otherwise
        final String providerId;   // e.g. "openai" — OpenCode only
        final String modelId;      // e.g. "gpt-5.5" — OpenCode only

        AgentChoice(String label, String provider, String providerId, String modelId) {
            this.label = label;
            this.provider = provider;
            this.providerId = providerId;
            this.modelId = modelId;
        }

        @Override public String toString() { return label; }

        boolean isClaude() { return provider == null || "claude".equals(provider); }
    }

    private final Project project;

    public NewSessionDialog(@Nullable Project project) {
        super(project);
        this.project = project;
        this.enabledServers = ServerRegistry.getInstance().getEnabledServers();
        setTitle("New be-conductor Session");
        loadServerConfig();
        init();
    }

    private void loadServerConfig() {
        try {
            BeConductorClient client = BeConductorClient.getInstance();
            ApiModels.ConfigResponse config = client.getConfig(selectedServerKey);
            if (config.allowed_commands != null && !config.allowed_commands.isEmpty()) {
                serverCommands = config.allowed_commands;
            }
        } catch (Exception ignored) {
            // Server unreachable — use fallback
        }
    }

    @Override
    @Nullable
    protected JComponent createCenterPanel() {
        JPanel panel = new JPanel(new GridBagLayout());
        GridBagConstraints c = new GridBagConstraints();
        c.insets = new Insets(4, 4, 4, 8);
        c.anchor = GridBagConstraints.WEST;

        int row = 0;

        // Row: Server (only shown when multi-server)
        if (enabledServers.size() > 1) {
            c.gridx = 0;
            c.gridy = row;
            c.fill = GridBagConstraints.NONE;
            c.weightx = 0;
            panel.add(new JBLabel("Server:"), c);

            String[] serverLabels = enabledServers.stream()
                    .map(s -> s.label).toArray(String[]::new);
            serverCombo = new JComboBox<>(serverLabels);
            serverCombo.addActionListener(e -> {
                int idx = serverCombo.getSelectedIndex();
                if (idx >= 0 && idx < enabledServers.size()) {
                    selectedServerKey = enabledServers.get(idx).key;
                    serverCommands = null;
                    loadServerConfig();
                }
            });
            c.gridx = 1;
            c.fill = GridBagConstraints.HORIZONTAL;
            c.weightx = 1;
            panel.add(serverCombo, c);
            row++;
        }

        // Row: Command
        c.gridx = 0;
        c.gridy = row;
        c.fill = GridBagConstraints.NONE;
        c.weightx = 0;
        panel.add(new JBLabel("Command:"), c);

        String[] commandLabels;
        if (serverCommands != null) {
            commandLabels = serverCommands.stream()
                    .map(cmd -> cmd.label != null ? cmd.label : cmd.command)
                    .toArray(String[]::new);
        } else {
            commandLabels = FALLBACK_AGENTS;
        }
        commandCombo = new JComboBox<>(commandLabels);
        // Select default
        for (int i = 0; i < commandLabels.length; i++) {
            if (commandLabels[i].toLowerCase().contains(DEFAULT_AGENT)) {
                commandCombo.setSelectedIndex(i);
                break;
            }
        }
        c.gridx = 1;
        c.gridwidth = 2;
        c.fill = GridBagConstraints.HORIZONTAL;
        c.weightx = 1.0;
        panel.add(commandCombo, c);

        // Row: Session name
        row++;
        c.gridx = 0;
        c.gridy = row;
        c.gridwidth = 1;
        c.fill = GridBagConstraints.NONE;
        c.weightx = 0;
        panel.add(new JBLabel("Session name:"), c);

        nameField = new JBTextField(20);
        nameField.getEmptyText().setText("e.g. feature-auth");
        c.gridx = 1;
        c.gridwidth = 2;
        c.fill = GridBagConstraints.HORIZONTAL;
        c.weightx = 1.0;
        panel.add(nameField, c);

        // Row: Working directory
        row++;
        c.gridx = 0;
        c.gridy = row;
        c.gridwidth = 1;
        c.fill = GridBagConstraints.NONE;
        c.weightx = 0;
        panel.add(new JBLabel("Working dir:"), c);

        cwdField = new TextFieldWithBrowseButton();
        cwdField.addBrowseFolderListener(
                "Select Working Directory", null, project,
                FileChooserDescriptorFactory.createSingleFolderDescriptor()
        );
        String defaultPath = project != null ? project.getBasePath() : System.getProperty("user.home");
        if (defaultPath != null) {
            cwdField.setText(defaultPath);
        }
        c.gridx = 1;
        c.gridwidth = 2;
        c.fill = GridBagConstraints.HORIZONTAL;
        c.weightx = 1.0;
        panel.add(cwdField, c);

        // Row: Session type
        row++;
        c.gridx = 0;
        c.gridy = row;
        c.gridwidth = 1;
        c.fill = GridBagConstraints.NONE;
        c.weightx = 0;
        panel.add(new JBLabel("Type:"), c);

        sessionTypeCombo = new JComboBox<>(new String[]{"GUI (Editor tab)", "GUI (Panel)", "Terminal"});
        sessionTypeCombo.setSelectedIndex(0);
        c.gridx = 1;
        c.gridwidth = 2;
        c.fill = GridBagConstraints.HORIZONTAL;
        c.weightx = 1.0;
        panel.add(sessionTypeCombo, c);

        // Row: Agent picker (only meaningful in GUI mode)
        row++;
        c.gridx = 0;
        c.gridy = row;
        c.gridwidth = 1;
        c.fill = GridBagConstraints.NONE;
        c.weightx = 0;
        agentLabel = new JBLabel("Agent:");
        panel.add(agentLabel, c);

        agentCombo = new JComboBox<>();
        agentCombo.addItem(new AgentChoice("Claude (native)", null, null, null));
        c.gridx = 1;
        c.gridwidth = 2;
        c.fill = GridBagConstraints.HORIZONTAL;
        c.weightx = 1.0;
        panel.add(agentCombo, c);

        // Row: Agent status (server URL + model count, when an
        // OpenCode entry is selected; hidden otherwise)
        row++;
        c.gridx = 1;
        c.gridy = row;
        c.gridwidth = 2;
        c.fill = GridBagConstraints.HORIZONTAL;
        c.weightx = 1.0;
        agentStatus = new JBLabel(" ");
        agentStatus.setForeground(UIManager.getColor("Label.disabledForeground"));
        agentStatus.setVisible(false);
        panel.add(agentStatus, c);

        // Visibility & status sync.
        sessionTypeCombo.addActionListener(e -> updateAgentRowVisibility());
        agentCombo.addActionListener(e -> updateAgentStatusVisibility());

        // Kick off the OpenCode model fetch in the background — this
        // populates the dropdown with one entry per OpenCode model the
        // server reports. We do this async so the dialog opens
        // instantly even if OpenCode is slow / unreachable.
        loadOpenCodeModelsAsync();

        // Row: Worktree checkbox + git status
        row++;
        c.gridx = 0;
        c.gridy = row;
        c.gridwidth = 1;
        c.fill = GridBagConstraints.NONE;
        c.weightx = 0;
        panel.add(new JBLabel(""), c); // spacer

        worktreeCheckbox = new JBCheckBox("Isolate with git worktree");
        worktreeCheckbox.setEnabled(false);
        c.gridx = 1;
        c.gridwidth = 1;
        c.fill = GridBagConstraints.HORIZONTAL;
        c.weightx = 0;
        panel.add(worktreeCheckbox, c);

        gitStatus = new JBLabel("");
        gitStatus.setForeground(UIManager.getColor("Label.disabledForeground"));
        c.gridx = 2;
        c.gridwidth = 1;
        c.fill = GridBagConstraints.NONE;
        c.weightx = 0;
        panel.add(gitStatus, c);

        // Row: Branch preview
        row++;
        c.gridx = 1;
        c.gridy = row;
        c.gridwidth = 2;
        c.fill = GridBagConstraints.HORIZONTAL;
        c.weightx = 1.0;
        branchPreview = new JBLabel("");
        branchPreview.setForeground(UIManager.getColor("Label.disabledForeground"));
        panel.add(branchPreview, c);

        // Listeners
        cwdField.getTextField().getDocument().addDocumentListener(new SimpleDocumentListener(this::checkGitDirectory));
        nameField.getDocument().addDocumentListener(new SimpleDocumentListener(this::updateBranchPreview));
        worktreeCheckbox.addChangeListener(e -> updateBranchPreview());

        // Initial git check
        checkGitDirectory();

        // Initial agent-row visibility (default GUI -> visible).
        updateAgentRowVisibility();

        panel.setPreferredSize(new Dimension(480, panel.getPreferredSize().height));
        return panel;
    }

    private void checkGitDirectory() {
        String path = cwdField.getText().trim();
        if (path.isEmpty()) {
            gitStatus.setText("");
            worktreeCheckbox.setEnabled(false);
            isGitRepo = false;
            updateBranchPreview();
            return;
        }

        gitStatus.setText("checking...");
        // Run git check in background to avoid blocking the UI
        new Thread(() -> {
            try {
                BeConductorClient client = BeConductorClient.getInstance();
                ApiModels.GitCheckResponse resp = client.checkGit(selectedServerKey, path);
                SwingUtilities.invokeLater(() -> {
                    isGitRepo = resp.is_git;
                    currentBranch = resp.current_branch != null ? resp.current_branch : "";
                    if (resp.is_git) {
                        String text = "git repo";
                        if (resp.existing_worktrees > 0) {
                            text += " (" + resp.existing_worktrees + " worktree"
                                    + (resp.existing_worktrees > 1 ? "s" : "") + ")";
                        }
                        gitStatus.setText(text);
                        worktreeCheckbox.setEnabled(true);
                    } else {
                        gitStatus.setText("not a git repo");
                        worktreeCheckbox.setEnabled(false);
                        worktreeCheckbox.setSelected(false);
                    }
                    updateBranchPreview();
                });
            } catch (Exception e) {
                SwingUtilities.invokeLater(() -> {
                    gitStatus.setText("server offline");
                    worktreeCheckbox.setEnabled(false);
                    isGitRepo = false;
                    updateBranchPreview();
                });
            }
        }).start();
    }

    private void updateBranchPreview() {
        if (worktreeCheckbox.isSelected() && !nameField.getText().trim().isEmpty()) {
            String safeName = nameField.getText().trim().replaceAll("[^a-zA-Z0-9-]", "-");
            branchPreview.setText("Branch: be-conductor/" + safeName
                    + (currentBranch.isEmpty() ? "" : " (from " + currentBranch + ")"));
        } else {
            branchPreview.setText("");
        }
    }

    @Override
    @Nullable
    protected ValidationInfo doValidate() {
        String name = nameField.getText().trim();
        if (name.isEmpty()) {
            return new ValidationInfo("Session name must not be empty.", nameField);
        }
        if (!name.matches("[a-zA-Z0-9][a-zA-Z0-9 _.~-]{0,63}")) {
            return new ValidationInfo(
                "Must start with a letter or digit, max 64 chars (letters, digits, spaces, hyphens, underscores, dots, tildes).",
                nameField);
        }
        if (worktreeCheckbox.isSelected() && !isGitRepo) {
            return new ValidationInfo("Working directory is not a git repository.", cwdField);
        }
        return null;
    }

    @Override
    @Nullable
    public JComponent getPreferredFocusedComponent() {
        return nameField;
    }

    public String getSessionName() {
        return nameField.getText().trim();
    }

    public String getCommand() {
        int idx = commandCombo.getSelectedIndex();
        if (serverCommands != null && idx >= 0 && idx < serverCommands.size()) {
            return serverCommands.get(idx).command;
        }
        return (String) commandCombo.getSelectedItem();
    }

    public String getWorkingDirectory() {
        return cwdField.getText().trim();
    }

    public boolean isWorktreeEnabled() {
        return worktreeCheckbox.isSelected();
    }

    /** @return "pty" or "agent" */
    public String getSessionType() {
        int idx = sessionTypeCombo.getSelectedIndex();
        return idx <= 1 ? "agent" : "pty";  // 0=GUI editor, 1=GUI panel, 2=terminal
    }

    /** @return "editor" or "panel" (only meaningful for agent sessions). */
    public String getOpenMode() {
        return sessionTypeCombo.getSelectedIndex() == 1 ? "panel" : "editor";
    }

    /** @return server key for the selected server. */
    public String getServerKey() {
        return selectedServerKey;
    }

    /**
     * Build agent_options for the run-session API. Returns null when
     * the user picked Claude (native) — the backend treats missing
     * agent_options.provider as the legacy Claude path. For OpenCode
     * picks, returns a map matching the dashboard's body shape.
     */
    public java.util.Map<String, Object> getAgentOptions() {
        if (agentCombo == null) return null;
        Object sel = agentCombo.getSelectedItem();
        if (!(sel instanceof AgentChoice)) return null;
        AgentChoice choice = (AgentChoice) sel;
        if (choice.isClaude()) return null;
        java.util.Map<String, Object> opts = new java.util.HashMap<>();
        opts.put("provider", choice.provider);
        if (choice.providerId != null) opts.put("opencode_provider_id", choice.providerId);
        if (choice.modelId != null) opts.put("opencode_model_id", choice.modelId);
        return opts;
    }

    /** Hide the Agent row entirely when not in GUI mode. */
    private void updateAgentRowVisibility() {
        boolean isAgent = "agent".equals(getSessionType());
        if (agentLabel != null) agentLabel.setVisible(isAgent);
        if (agentCombo != null) agentCombo.setVisible(isAgent);
        // Status visibility depends on both session type and selection.
        updateAgentStatusVisibility();
    }

    /**
     * The agent-status line shows "OpenCode at <url> — N models" only
     * when an OpenCode entry is the current pick. Hidden otherwise to
     * keep the dialog calm when the user is on Claude.
     */
    private void updateAgentStatusVisibility() {
        if (agentStatus == null) return;
        boolean isAgent = "agent".equals(getSessionType());
        Object sel = agentCombo != null ? agentCombo.getSelectedItem() : null;
        boolean isOpenCode = sel instanceof AgentChoice && !((AgentChoice) sel).isClaude();
        agentStatus.setVisible(isAgent && isOpenCode);
    }

    /** Fetch OpenCode models in the background and append them to the agent combo. */
    private void loadOpenCodeModelsAsync() {
        new Thread(() -> {
            ApiModels.AgentProviderModelsResponse resp = null;
            String error = null;
            try {
                BeConductorClient client = BeConductorClient.getInstance();
                resp = client.getAgentProviderModels(selectedServerKey, "opencode");
            } catch (Exception e) {
                error = e.getMessage();
            }
            final ApiModels.AgentProviderModelsResponse finalResp = resp;
            final String finalError = error;
            SwingUtilities.invokeLater(() -> {
                if (finalResp != null && finalResp.models != null && !finalResp.models.isEmpty()) {
                    for (ApiModels.AgentProviderModel m : finalResp.models) {
                        String label = "OpenCode • " + (m.label != null ? m.label : m.value);
                        agentCombo.addItem(new AgentChoice(label, "opencode", m.provider_id, m.model_id));
                    }
                    int n = finalResp.models.size();
                    String url = finalResp.url != null ? finalResp.url : "127.0.0.1:7798";
                    agentStatus.setText("OpenCode at " + url + " — " + n + " model" + (n == 1 ? "" : "s"));
                } else if (finalResp != null && finalResp.error != null) {
                    agentStatus.setText("OpenCode unreachable: " + finalResp.error);
                } else if (finalError != null) {
                    agentStatus.setText("OpenCode catalogue lookup failed: " + finalError);
                } else {
                    agentStatus.setText("OpenCode not running — start `opencode serve --port 7798` to see models.");
                }
                updateAgentStatusVisibility();
            });
        }, "be-conductor-opencode-models").start();
    }

    /** Simple listener that fires a callback on any document change. */
    private static class SimpleDocumentListener implements DocumentListener {
        private final Runnable callback;

        SimpleDocumentListener(Runnable callback) {
            this.callback = callback;
        }

        @Override public void insertUpdate(DocumentEvent e) { callback.run(); }
        @Override public void removeUpdate(DocumentEvent e) { callback.run(); }
        @Override public void changedUpdate(DocumentEvent e) { callback.run(); }
    }
}
