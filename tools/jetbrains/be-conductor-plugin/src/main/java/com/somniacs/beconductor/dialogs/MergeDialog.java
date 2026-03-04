package com.somniacs.beconductor.dialogs;

import com.intellij.notification.Notification;
import com.intellij.notification.NotificationType;
import com.intellij.notification.Notifications;
import com.intellij.openapi.application.ApplicationManager;
import com.intellij.openapi.project.Project;
import com.intellij.openapi.ui.DialogWrapper;
import com.intellij.ui.components.JBLabel;
import com.intellij.ui.components.JBTextArea;
import com.somniacs.beconductor.api.ApiModels;
import com.somniacs.beconductor.api.BeConductorClient;
import com.somniacs.beconductor.toolwindow.DiffViewerUtil;
import org.jetbrains.annotations.Nullable;

import javax.swing.*;
import java.awt.*;

/**
 * Dialog for choosing a merge strategy and optional commit message.
 * Shows a preview of what will be merged.
 */
public class MergeDialog extends DialogWrapper {

    private final Project project;
    private final ApiModels.WorktreeInfo worktree;
    private final ApiModels.MergePreview preview;

    private JRadioButton squashRadio;
    private JRadioButton mergeRadio;
    private JRadioButton rebaseRadio;
    private JBTextArea messageField;

    public MergeDialog(@Nullable Project project, ApiModels.WorktreeInfo worktree, ApiModels.MergePreview preview) {
        super(project);
        this.project = project;
        this.worktree = worktree;
        this.preview = preview;
        setTitle("Merge Worktree: " + worktree.name);
        setOKButtonText("Merge");
        init();
    }

    @Override
    @Nullable
    protected JComponent createCenterPanel() {
        JPanel panel = new JPanel();
        panel.setLayout(new BoxLayout(panel, BoxLayout.Y_AXIS));

        // Preview info
        JPanel infoPanel = new JPanel(new GridBagLayout());
        infoPanel.setBorder(BorderFactory.createTitledBorder("Merge Preview"));
        GridBagConstraints c = new GridBagConstraints();
        c.insets = new Insets(2, 4, 2, 8);
        c.anchor = GridBagConstraints.WEST;

        c.gridx = 0; c.gridy = 0;
        infoPanel.add(new JBLabel("Branch:"), c);
        c.gridx = 1;
        infoPanel.add(new JBLabel(worktree.branch + " → " + worktree.base_branch), c);

        c.gridx = 0; c.gridy = 1;
        infoPanel.add(new JBLabel("Commits:"), c);
        c.gridx = 1;
        infoPanel.add(new JBLabel(preview.commits_ahead + " ahead, " + preview.commits_behind + " behind"), c);

        if (preview.changed_files != null) {
            c.gridx = 0; c.gridy = 2;
            infoPanel.add(new JBLabel("Files changed:"), c);
            c.gridx = 1;
            JPanel fileRow = new JPanel(new FlowLayout(FlowLayout.LEFT, 4, 0));
            fileRow.setOpaque(false);
            fileRow.add(new JBLabel(String.valueOf(preview.changed_files.size())));
            JButton viewDiffBtn = new JButton("View Diff");
            viewDiffBtn.addActionListener(e -> openDiff());
            fileRow.add(viewDiffBtn);
            infoPanel.add(fileRow, c);
        }

        if (preview.conflict_files != null && !preview.conflict_files.isEmpty()) {
            c.gridx = 0; c.gridy = 3;
            JBLabel conflictLabel = new JBLabel("Conflicts:");
            conflictLabel.setForeground(Color.RED);
            infoPanel.add(conflictLabel, c);
            c.gridx = 1;
            JBLabel conflictNames = new JBLabel(String.join(", ", preview.conflict_files));
            conflictNames.setForeground(Color.RED);
            infoPanel.add(conflictNames, c);
        }

        infoPanel.setAlignmentX(Component.LEFT_ALIGNMENT);
        panel.add(infoPanel);
        panel.add(Box.createVerticalStrut(8));

        // Strategy selection
        JPanel strategyPanel = new JPanel();
        strategyPanel.setLayout(new BoxLayout(strategyPanel, BoxLayout.Y_AXIS));
        strategyPanel.setBorder(BorderFactory.createTitledBorder("Merge Strategy"));

        ButtonGroup group = new ButtonGroup();
        squashRadio = new JRadioButton("Squash — combine all commits into one");
        mergeRadio = new JRadioButton("Merge — create a merge commit");
        rebaseRadio = new JRadioButton("Rebase — replay commits onto base");
        squashRadio.setSelected(true);

        group.add(squashRadio);
        group.add(mergeRadio);
        group.add(rebaseRadio);

        strategyPanel.add(squashRadio);
        strategyPanel.add(mergeRadio);
        strategyPanel.add(rebaseRadio);
        strategyPanel.setAlignmentX(Component.LEFT_ALIGNMENT);
        panel.add(strategyPanel);
        panel.add(Box.createVerticalStrut(8));

        // Commit message (optional)
        JPanel msgPanel = new JPanel(new BorderLayout());
        msgPanel.setBorder(BorderFactory.createTitledBorder("Commit Message (optional)"));
        messageField = new JBTextArea(3, 40);
        messageField.setLineWrap(true);
        messageField.setWrapStyleWord(true);
        msgPanel.add(new JScrollPane(messageField), BorderLayout.CENTER);
        msgPanel.setAlignmentX(Component.LEFT_ALIGNMENT);
        panel.add(msgPanel);

        panel.setPreferredSize(new Dimension(500, panel.getPreferredSize().height));
        return panel;
    }

    private void openDiff() {
        ApplicationManager.getApplication().executeOnPooledThread(() -> {
            try {
                ApiModels.RichDiffResponse richDiff =
                        BeConductorClient.getInstance().getWorktreeRichDiff(worktree.name);
                if (richDiff.files == null || richDiff.files.isEmpty()) {
                    return;
                }
                SwingUtilities.invokeLater(() ->
                        DiffViewerUtil.showDiff(project, "Merge Preview: " + worktree.name,
                                richDiff.files)
                );
            } catch (Exception ex) {
                SwingUtilities.invokeLater(() ->
                        Notifications.Bus.notify(new Notification(
                                "be-conductor", "Diff Failed", ex.getMessage(),
                                NotificationType.ERROR
                        ))
                );
            }
        });
    }

    public String getStrategy() {
        if (mergeRadio.isSelected()) return "merge";
        if (rebaseRadio.isSelected()) return "rebase";
        return "squash";
    }

    public String getCommitMessage() {
        String msg = messageField.getText().trim();
        return msg.isEmpty() ? null : msg;
    }
}
