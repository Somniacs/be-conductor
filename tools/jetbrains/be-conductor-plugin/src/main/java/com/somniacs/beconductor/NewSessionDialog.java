package com.somniacs.beconductor;

import com.intellij.openapi.project.Project;
import com.intellij.openapi.ui.DialogWrapper;
import com.intellij.openapi.ui.ValidationInfo;
import com.intellij.ui.components.JBLabel;
import com.intellij.ui.components.JBTextField;
import org.jetbrains.annotations.Nullable;

import javax.swing.*;
import java.awt.*;

public class NewSessionDialog extends DialogWrapper {

    private static final String[] AGENTS = {
        "claude", "codex", "aider", "gemini", "copilot",
        "opencode", "amp", "goose", "forge", "cursor"
    };

    private static final String DEFAULT_AGENT = "claude";

    private JBTextField nameField;
    private JComboBox<String> agentCombo;

    public NewSessionDialog(@Nullable Project project) {
        super(project);
        setTitle("New be-conductor Session");
        init();
    }

    @Override
    @Nullable
    protected JComponent createCenterPanel() {
        JPanel panel = new JPanel(new GridBagLayout());
        GridBagConstraints c = new GridBagConstraints();
        c.insets = new Insets(4, 4, 4, 8);
        c.anchor = GridBagConstraints.WEST;

        // Row 0: Agent
        c.gridx = 0;
        c.gridy = 0;
        c.fill = GridBagConstraints.NONE;
        c.weightx = 0;
        panel.add(new JBLabel("Agent:"), c);

        agentCombo = new JComboBox<>(AGENTS);
        agentCombo.setSelectedItem(DEFAULT_AGENT);
        c.gridx = 1;
        c.fill = GridBagConstraints.HORIZONTAL;
        c.weightx = 1.0;
        panel.add(agentCombo, c);

        // Row 1: Session name
        c.gridx = 0;
        c.gridy = 1;
        c.fill = GridBagConstraints.NONE;
        c.weightx = 0;
        panel.add(new JBLabel("Session name:"), c);

        nameField = new JBTextField(20);
        nameField.getEmptyText().setText("e.g. feature-auth");
        c.gridx = 1;
        c.fill = GridBagConstraints.HORIZONTAL;
        c.weightx = 1.0;
        panel.add(nameField, c);

        panel.setPreferredSize(new Dimension(340, panel.getPreferredSize().height));
        return panel;
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
                "Must start with a letter or digit, max 64 chars (letters, digits, spaces, hyphens, underscores, dots, tildes).", nameField);
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

    public String getAgent() {
        return (String) agentCombo.getSelectedItem();
    }
}
