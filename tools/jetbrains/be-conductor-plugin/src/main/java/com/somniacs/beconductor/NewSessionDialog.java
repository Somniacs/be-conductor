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

    private JBTextField nameField;
    private JComboBox<String> commandCombo;
    private TextFieldWithBrowseButton cwdField;
    private JBCheckBox worktreeCheckbox;
    private JBLabel branchPreview;
    private JBLabel gitStatus;

    private List<ApiModels.CommandConfig> serverCommands;
    private boolean isGitRepo = false;
    private String currentBranch = "";

    private final Project project;

    public NewSessionDialog(@Nullable Project project) {
        super(project);
        this.project = project;
        setTitle("New be-conductor Session");
        loadServerConfig();
        init();
    }

    private void loadServerConfig() {
        try {
            BeConductorClient client = BeConductorClient.getInstance();
            ApiModels.ConfigResponse config = client.getConfig();
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

        // Row 0: Command
        c.gridx = 0;
        c.gridy = 0;
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

        // Row 1: Session name
        c.gridx = 0;
        c.gridy = 1;
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

        // Row 2: Working directory
        c.gridx = 0;
        c.gridy = 2;
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

        // Row 3: Worktree checkbox + git status
        c.gridx = 0;
        c.gridy = 3;
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

        // Row 4: Branch preview
        c.gridx = 1;
        c.gridy = 4;
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
                ApiModels.GitCheckResponse resp = client.checkGit(path);
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
