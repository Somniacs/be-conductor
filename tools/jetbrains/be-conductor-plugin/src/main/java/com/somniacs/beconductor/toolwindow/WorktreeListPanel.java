package com.somniacs.beconductor.toolwindow;

import com.intellij.icons.AllIcons;
import com.intellij.notification.Notification;
import com.intellij.notification.NotificationType;
import com.intellij.notification.Notifications;
import com.intellij.openapi.application.ApplicationManager;
import com.intellij.openapi.project.Project;
import com.intellij.ui.SimpleColoredComponent;
import com.intellij.ui.SimpleTextAttributes;
import com.intellij.ui.components.JBList;
import com.intellij.ui.components.JBScrollPane;
import com.intellij.util.Alarm;
import com.somniacs.beconductor.api.ApiModels;
import com.somniacs.beconductor.api.BeConductorClient;
import com.somniacs.beconductor.dialogs.MergeDialog;

import javax.swing.*;
import java.awt.*;
import java.awt.event.MouseAdapter;
import java.awt.event.MouseEvent;
import java.util.List;

public class WorktreeListPanel extends JPanel {

    private static final int REFRESH_INTERVAL_MS = 5000;

    private final Project project;
    private final DefaultListModel<ApiModels.WorktreeInfo> listModel;
    private final JBList<ApiModels.WorktreeInfo> worktreeList;
    private final Alarm refreshAlarm;
    private final JLabel statusLabel;

    public WorktreeListPanel(Project project) {
        super(new BorderLayout());
        this.project = project;

        // Toolbar
        JPanel toolbar = new JPanel(new FlowLayout(FlowLayout.LEFT, 4, 2));
        JButton refreshBtn = new JButton("Refresh");
        refreshBtn.addActionListener(e -> refresh());
        toolbar.add(refreshBtn);

        JButton gcBtn = new JButton("GC");
        gcBtn.setToolTipText("Garbage-collect stale worktrees");
        gcBtn.addActionListener(e -> runGC());
        toolbar.add(gcBtn);

        add(toolbar, BorderLayout.NORTH);

        // List
        listModel = new DefaultListModel<>();
        worktreeList = new JBList<>(listModel);
        worktreeList.setCellRenderer(new WorktreeCellRenderer());
        worktreeList.setSelectionMode(ListSelectionModel.SINGLE_SELECTION);
        add(new JBScrollPane(worktreeList), BorderLayout.CENTER);

        // Status
        statusLabel = new JLabel(" ");
        statusLabel.setBorder(BorderFactory.createEmptyBorder(2, 8, 2, 8));
        add(statusLabel, BorderLayout.SOUTH);

        // Context menu
        worktreeList.addMouseListener(new MouseAdapter() {
            @Override
            public void mousePressed(MouseEvent e) { showPopup(e); }
            @Override
            public void mouseReleased(MouseEvent e) { showPopup(e); }

            private void showPopup(MouseEvent e) {
                if (!e.isPopupTrigger()) return;
                int index = worktreeList.locationToIndex(e.getPoint());
                if (index < 0) return;
                worktreeList.setSelectedIndex(index);
                ApiModels.WorktreeInfo wt = listModel.get(index);
                createContextMenu(wt).show(worktreeList, e.getX(), e.getY());
            }
        });

        // Auto-refresh
        refreshAlarm = new Alarm(Alarm.ThreadToUse.POOLED_THREAD, project);
        scheduleRefresh();
        refresh();
    }

    private void scheduleRefresh() {
        if (project.isDisposed()) return;
        refreshAlarm.addRequest(() -> {
            refresh();
            scheduleRefresh();
        }, REFRESH_INTERVAL_MS);
    }

    public void refresh() {
        ApplicationManager.getApplication().executeOnPooledThread(() -> {
            try {
                BeConductorClient client = BeConductorClient.getInstance();
                List<ApiModels.WorktreeInfo> worktrees = client.listWorktrees();
                SwingUtilities.invokeLater(() -> {
                    listModel.clear();
                    for (ApiModels.WorktreeInfo wt : worktrees) {
                        listModel.addElement(wt);
                    }
                    statusLabel.setText(worktrees.size() + " worktree(s)");
                });
            } catch (Exception e) {
                SwingUtilities.invokeLater(() -> {
                    listModel.clear();
                    statusLabel.setText("Server offline");
                });
            }
        });
    }

    private JPopupMenu createContextMenu(ApiModels.WorktreeInfo wt) {
        JPopupMenu menu = new JPopupMenu();

        if ("active".equals(wt.status)) {
            JMenuItem finalizeItem = new JMenuItem("Finalize");
            finalizeItem.addActionListener(e -> finalizeWorktree(wt.name));
            menu.add(finalizeItem);
        }

        JMenuItem diffItem = new JMenuItem("View Diff");
        diffItem.addActionListener(e -> viewDiff(wt.name));
        menu.add(diffItem);

        if ("active".equals(wt.status) || "finalized".equals(wt.status)) {
            JMenuItem mergeItem = new JMenuItem("Merge...");
            mergeItem.addActionListener(e -> mergeWorktree(wt));
            menu.add(mergeItem);
        }

        menu.addSeparator();

        JMenuItem deleteItem = new JMenuItem("Delete");
        deleteItem.addActionListener(e -> deleteWorktree(wt));
        menu.add(deleteItem);

        return menu;
    }

    private void finalizeWorktree(String name) {
        ApplicationManager.getApplication().executeOnPooledThread(() -> {
            try {
                ApiModels.WorktreeInfo result = BeConductorClient.getInstance().finalizeWorktree(name);
                SwingUtilities.invokeLater(() -> {
                    Notifications.Bus.notify(new Notification(
                            "be-conductor", "Worktree Finalized",
                            "'" + name + "' finalized. " + result.commits_ahead + " commit(s) ahead.",
                            NotificationType.INFORMATION
                    ));
                    refresh();
                });
            } catch (Exception e) {
                SwingUtilities.invokeLater(() ->
                        Notifications.Bus.notify(new Notification(
                                "be-conductor", "Finalize Failed", e.getMessage(),
                                NotificationType.ERROR
                        ))
                );
            }
        });
    }

    private void viewDiff(String name) {
        ApplicationManager.getApplication().executeOnPooledThread(() -> {
            try {
                ApiModels.RichDiffResponse richDiff =
                        BeConductorClient.getInstance().getWorktreeRichDiff(name);

                if (richDiff.files == null || richDiff.files.isEmpty()) {
                    SwingUtilities.invokeLater(() ->
                            Notifications.Bus.notify(new Notification(
                                    "be-conductor", "No Changes",
                                    "Worktree '" + name + "' has no changes.",
                                    NotificationType.INFORMATION
                            ))
                    );
                    return;
                }

                SwingUtilities.invokeLater(() ->
                        DiffViewerUtil.showDiff(project, "Diff: " + name, richDiff.files)
                );
            } catch (Exception e) {
                // Fall back to plain text diff (e.g. older server without format=rich)
                viewDiffFallback(name);
            }
        });
    }

    private void viewDiffFallback(String name) {
        try {
            ApiModels.DiffResponse diff = BeConductorClient.getInstance().getWorktreeDiff(name);
            String content = diff.diff != null ? diff.diff : "(no changes)";
            SwingUtilities.invokeLater(() -> {
                JTextArea textArea = new JTextArea(content);
                textArea.setFont(new Font(Font.MONOSPACED, Font.PLAIN, 12));
                textArea.setEditable(false);
                JScrollPane scrollPane = new JScrollPane(textArea);
                scrollPane.setPreferredSize(new Dimension(800, 600));

                JDialog dialog = new JDialog();
                dialog.setTitle("Diff: " + name);
                dialog.setContentPane(scrollPane);
                dialog.pack();
                dialog.setLocationRelativeTo(null);
                dialog.setVisible(true);
            });
        } catch (Exception e) {
            SwingUtilities.invokeLater(() ->
                    Notifications.Bus.notify(new Notification(
                            "be-conductor", "Diff Failed", e.getMessage(),
                            NotificationType.ERROR
                    ))
            );
        }
    }

    private void mergeWorktree(ApiModels.WorktreeInfo wt) {
        // Fetch preview in background, then show merge dialog on EDT
        ApplicationManager.getApplication().executeOnPooledThread(() -> {
            try {
                ApiModels.MergePreview preview = BeConductorClient.getInstance().previewMerge(wt.name);
                SwingUtilities.invokeLater(() -> {
                    if (!preview.can_merge) {
                        Notifications.Bus.notify(new Notification(
                                "be-conductor", "Nothing to Merge",
                                preview.message != null ? preview.message : "No changes to merge.",
                                NotificationType.WARNING
                        ));
                        return;
                    }

                    MergeDialog dialog = new MergeDialog(project, wt, preview);
                    if (dialog.showAndGet()) {
                        executeMerge(wt.name, dialog.getStrategy(), dialog.getCommitMessage());
                    }
                });
            } catch (Exception e) {
                SwingUtilities.invokeLater(() ->
                        Notifications.Bus.notify(new Notification(
                                "be-conductor", "Merge Preview Failed", e.getMessage(),
                                NotificationType.ERROR
                        ))
                );
            }
        });
    }

    private void executeMerge(String name, String strategy, String message) {
        ApplicationManager.getApplication().executeOnPooledThread(() -> {
            try {
                ApiModels.MergeResult result = BeConductorClient.getInstance()
                        .executeMerge(name, strategy, message);
                SwingUtilities.invokeLater(() -> {
                    if (result.success) {
                        Notifications.Bus.notify(new Notification(
                                "be-conductor", "Merge Complete",
                                "Merged '" + name + "' into " + result.target_branch
                                        + " (" + result.strategy + "): " + result.commits_merged + " commit(s)",
                                NotificationType.INFORMATION
                        ));
                    } else {
                        Notifications.Bus.notify(new Notification(
                                "be-conductor", "Merge Failed",
                                result.message != null ? result.message : "Unknown error",
                                NotificationType.ERROR
                        ));
                    }
                    refresh();
                });
            } catch (Exception e) {
                SwingUtilities.invokeLater(() ->
                        Notifications.Bus.notify(new Notification(
                                "be-conductor", "Merge Failed", e.getMessage(),
                                NotificationType.ERROR
                        ))
                );
            }
        });
    }

    private void deleteWorktree(ApiModels.WorktreeInfo wt) {
        int result = JOptionPane.showConfirmDialog(
                this,
                "Delete worktree '" + wt.name + "' and its branch '" + wt.branch + "'?",
                "Delete Worktree",
                JOptionPane.YES_NO_OPTION,
                JOptionPane.WARNING_MESSAGE
        );
        if (result != JOptionPane.YES_OPTION) return;

        ApplicationManager.getApplication().executeOnPooledThread(() -> {
            try {
                BeConductorClient.getInstance().deleteWorktree(wt.name, false);
                SwingUtilities.invokeLater(() -> {
                    Notifications.Bus.notify(new Notification(
                            "be-conductor", "Worktree Deleted",
                            "'" + wt.name + "' removed.",
                            NotificationType.INFORMATION
                    ));
                    refresh();
                });
            } catch (Exception e) {
                SwingUtilities.invokeLater(() ->
                        Notifications.Bus.notify(new Notification(
                                "be-conductor", "Delete Failed", e.getMessage(),
                                NotificationType.ERROR
                        ))
                );
            }
        });
    }

    private void runGC() {
        ApplicationManager.getApplication().executeOnPooledThread(() -> {
            try {
                BeConductorClient client = BeConductorClient.getInstance();
                // Dry run first
                List<?> preview = client.worktreeGC(true, 7.0);
                if (preview == null || preview.isEmpty()) {
                    SwingUtilities.invokeLater(() ->
                            Notifications.Bus.notify(new Notification(
                                    "be-conductor", "GC", "No stale worktrees to clean up.",
                                    NotificationType.INFORMATION
                            ))
                    );
                    return;
                }

                SwingUtilities.invokeLater(() -> {
                    int result = JOptionPane.showConfirmDialog(
                            this,
                            "Remove " + preview.size() + " stale worktree(s)?",
                            "Garbage Collect",
                            JOptionPane.YES_NO_OPTION
                    );
                    if (result != JOptionPane.YES_OPTION) return;

                    ApplicationManager.getApplication().executeOnPooledThread(() -> {
                        try {
                            client.worktreeGC(false, 7.0);
                            SwingUtilities.invokeLater(() -> {
                                Notifications.Bus.notify(new Notification(
                                        "be-conductor", "GC Complete",
                                        "Cleaned up " + preview.size() + " worktree(s).",
                                        NotificationType.INFORMATION
                                ));
                                refresh();
                            });
                        } catch (Exception ex) {
                            SwingUtilities.invokeLater(() ->
                                    Notifications.Bus.notify(new Notification(
                                            "be-conductor", "GC Failed", ex.getMessage(),
                                            NotificationType.ERROR
                                    ))
                            );
                        }
                    });
                });
            } catch (Exception e) {
                SwingUtilities.invokeLater(() ->
                        Notifications.Bus.notify(new Notification(
                                "be-conductor", "GC Failed", e.getMessage(),
                                NotificationType.ERROR
                        ))
                );
            }
        });
    }

    /** Custom cell renderer for worktree items. */
    private static class WorktreeCellRenderer extends DefaultListCellRenderer {
        @Override
        public Component getListCellRendererComponent(JList<?> list, Object value, int index,
                                                       boolean isSelected, boolean cellHasFocus) {
            if (!(value instanceof ApiModels.WorktreeInfo wt)) {
                return super.getListCellRendererComponent(list, value, index, isSelected, cellHasFocus);
            }

            SimpleColoredComponent component = new SimpleColoredComponent();
            component.setOpaque(true);

            if (isSelected) {
                component.setBackground(list.getSelectionBackground());
                component.setForeground(list.getSelectionForeground());
            } else {
                component.setBackground(list.getBackground());
                component.setForeground(list.getForeground());
            }

            // Status icon
            switch (wt.status) {
                case "active" -> component.setIcon(AllIcons.Vcs.Branch);
                case "finalized" -> component.setIcon(AllIcons.RunConfigurations.TestPassed);
                default -> component.setIcon(AllIcons.General.Warning);
            }

            // Name
            component.append(wt.name, SimpleTextAttributes.REGULAR_BOLD_ATTRIBUTES);

            // Branch
            component.append("  " + wt.branch, SimpleTextAttributes.GRAYED_ATTRIBUTES);

            // Commits ahead
            if (wt.commits_ahead > 0) {
                component.append("  " + wt.commits_ahead + " ahead",
                        SimpleTextAttributes.GRAYED_ITALIC_ATTRIBUTES);
            }

            // Status badge
            if (!"active".equals(wt.status)) {
                component.append("  [" + wt.status + "]", SimpleTextAttributes.GRAYED_ITALIC_ATTRIBUTES);
            }

            return component;
        }
    }
}
