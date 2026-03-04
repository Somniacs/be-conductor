package com.somniacs.beconductor.toolwindow;

import com.intellij.icons.AllIcons;
import com.intellij.ide.util.PropertiesComponent;
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
import com.intellij.ide.BrowserUtil;
import com.intellij.openapi.actionSystem.ActionManager;
import com.intellij.openapi.actionSystem.AnAction;
import com.intellij.openapi.actionSystem.AnActionEvent;
import com.intellij.openapi.actionSystem.DataContext;
import com.intellij.openapi.diagnostic.Logger;
import com.somniacs.beconductor.api.ApiModels;
import com.somniacs.beconductor.api.BeConductorClient;
import com.somniacs.beconductor.dialogs.MergeDialog;
import org.jetbrains.plugins.terminal.ShellTerminalWidget;
import org.jetbrains.plugins.terminal.TerminalToolWindowManager;

import javax.swing.*;
import javax.swing.event.ListSelectionEvent;
import java.awt.*;
import java.awt.event.MouseAdapter;
import java.awt.event.MouseEvent;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

public class SessionListPanel extends JPanel {

    private static final Logger LOG = Logger.getInstance(SessionListPanel.class);
    private static final int REFRESH_INTERVAL_MS = 5000;
    private static final String TRACKED_KEY = "be-conductor.trackedSessions";

    /** Sessions that already have an attached terminal in this IDE. */
    private static final Set<String> attachedSessions = new HashSet<>();

    // ── Session persistence (survives IDE restart) ──────────────────────

    public static void trackSession(Project project, String name) {
        PropertiesComponent props = PropertiesComponent.getInstance(project);
        List<String> tracked = new java.util.ArrayList<>(getTrackedSessions(project));
        if (!tracked.contains(name)) {
            tracked.add(name);
            props.setValue(TRACKED_KEY, String.join(",", tracked));
        }
    }

    public static void untrackSession(Project project, String name) {
        PropertiesComponent props = PropertiesComponent.getInstance(project);
        List<String> tracked = new java.util.ArrayList<>(getTrackedSessions(project));
        tracked.remove(name);
        if (tracked.isEmpty()) {
            props.unsetValue(TRACKED_KEY);
        } else {
            props.setValue(TRACKED_KEY, String.join(",", tracked));
        }
    }

    public static List<String> getTrackedSessions(Project project) {
        PropertiesComponent props = PropertiesComponent.getInstance(project);
        String val = props.getValue(TRACKED_KEY);
        if (val == null || val.isEmpty()) return java.util.Collections.emptyList();
        return java.util.Arrays.asList(val.split(","));
    }

    public static void saveTrackedSessions(Project project, List<String> names) {
        PropertiesComponent props = PropertiesComponent.getInstance(project);
        if (names.isEmpty()) {
            props.unsetValue(TRACKED_KEY);
        } else {
            props.setValue(TRACKED_KEY, String.join(",", names));
        }
    }

    private final Project project;
    private final DefaultListModel<ApiModels.SessionResponse> listModel;
    private final JBList<ApiModels.SessionResponse> sessionList;
    private final Alarm refreshAlarm;
    private final JLabel statusLabel;

    // Toolbar action buttons
    private final JButton attachBtn;
    private final JButton resumeBtn;
    private final JButton stopBtn;
    private final JButton killBtn;
    private final JButton dismissBtn;
    // Worktree action buttons
    private final JButton diffBtn;
    private final JButton mergeBtn;
    private final JButton finalizeBtn;

    public SessionListPanel(Project project) {
        super(new BorderLayout());
        this.project = project;

        // === List (initialize early so toolbar lambdas can reference it) ===
        listModel = new DefaultListModel<>();
        sessionList = new JBList<>(listModel);
        sessionList.setCellRenderer(new SessionCellRenderer());
        sessionList.setSelectionMode(ListSelectionModel.SINGLE_SELECTION);

        // === Toolbar with action buttons ===
        JPanel toolbar = new JPanel();
        toolbar.setLayout(new BoxLayout(toolbar, BoxLayout.Y_AXIS));

        // Row 1: New Session, Dashboard, Refresh
        JPanel topActions = new JPanel(new FlowLayout(FlowLayout.LEFT, 2, 1));

        JButton newSessionBtn = createToolbarButton("New Session", AllIcons.General.Add,
                "Start a new AI agent session");
        newSessionBtn.addActionListener(e -> {
            AnAction action = ActionManager.getInstance().getAction("BeConductor.RunSession");
            if (action != null) {
                DataContext ctx = dataId -> com.intellij.openapi.actionSystem.CommonDataKeys.PROJECT.is(dataId) ? project : null;
                action.actionPerformed(AnActionEvent.createFromAnAction(
                        action, null, "be-conductor", ctx));
            }
        });
        topActions.add(newSessionBtn);

        JButton dashboardBtn = createToolbarButton("Dashboard", AllIcons.General.Web,
                "Open be-conductor web dashboard");
        dashboardBtn.addActionListener(e -> BrowserUtil.browse("http://127.0.0.1:7777"));
        topActions.add(dashboardBtn);

        topActions.add(Box.createHorizontalStrut(8));

        JButton refreshBtn = createToolbarButton(null, AllIcons.Actions.Refresh, "Refresh");
        refreshBtn.addActionListener(e -> refresh());
        topActions.add(refreshBtn);

        toolbar.add(topActions);

        // Row 2: Session actions
        JPanel sessionActions = new JPanel(new FlowLayout(FlowLayout.LEFT, 2, 1));

        attachBtn = createToolbarButton("Attach", AllIcons.Debugger.Console,
                "Open terminal attached to this session");
        attachBtn.addActionListener(e -> {
            ApiModels.SessionResponse s = sessionList.getSelectedValue();
            if (s != null) attachSession(s.name);
        });
        sessionActions.add(attachBtn);

        resumeBtn = createToolbarButton("Resume", AllIcons.Actions.Execute,
                "Resume this session (double-click also works)");
        resumeBtn.addActionListener(e -> {
            ApiModels.SessionResponse s = sessionList.getSelectedValue();
            if (s != null) resumeSession(s.id, s.name);
        });
        sessionActions.add(resumeBtn);

        stopBtn = createToolbarButton("Stop", AllIcons.Actions.Suspend,
                "Gracefully stop (session stays resumable)");
        stopBtn.addActionListener(e -> {
            ApiModels.SessionResponse s = sessionList.getSelectedValue();
            if (s == null) return;
            String mode = "stopping".equals(s.status) ? "kill" : "graceful";
            stopSession(s.id, mode);
        });
        sessionActions.add(stopBtn);

        killBtn = createToolbarButton("Kill", AllIcons.Actions.Cancel,
                "Force stop and remove session");
        killBtn.addActionListener(e -> {
            ApiModels.SessionResponse s = sessionList.getSelectedValue();
            if (s != null) stopSession(s.id, "kill");
        });
        sessionActions.add(killBtn);

        dismissBtn = createToolbarButton("Dismiss", AllIcons.Actions.GC,
                "Remove this exited session");
        dismissBtn.addActionListener(e -> {
            ApiModels.SessionResponse s = sessionList.getSelectedValue();
            if (s != null) dismissSession(s.id);
        });
        sessionActions.add(dismissBtn);

        toolbar.add(sessionActions);

        // Row 2: Worktree actions
        JPanel worktreeActions = new JPanel(new FlowLayout(FlowLayout.LEFT, 2, 1));

        diffBtn = createToolbarButton("Diff", AllIcons.Actions.Diff,
                "View worktree diff");
        diffBtn.addActionListener(e -> {
            ApiModels.SessionResponse s = sessionList.getSelectedValue();
            if (s != null && s.worktree != null) viewDiff(s.name);
        });
        worktreeActions.add(diffBtn);

        mergeBtn = createToolbarButton("Merge", AllIcons.Vcs.Merge,
                "Merge worktree into base branch");
        mergeBtn.addActionListener(e -> {
            ApiModels.SessionResponse s = sessionList.getSelectedValue();
            if (s != null && s.worktree != null) mergeWorktree(s);
        });
        worktreeActions.add(mergeBtn);

        finalizeBtn = createToolbarButton("Finalize", AllIcons.Actions.Commit,
                "Finalize worktree (auto-commit and mark done)");
        finalizeBtn.addActionListener(e -> {
            ApiModels.SessionResponse s = sessionList.getSelectedValue();
            if (s != null && s.worktree != null) finalizeWorktree(s.name);
        });
        worktreeActions.add(finalizeBtn);

        toolbar.add(worktreeActions);
        add(toolbar, BorderLayout.NORTH);

        // === List (add to layout) ===
        add(new JBScrollPane(sessionList), BorderLayout.CENTER);

        // Status bar
        statusLabel = new JLabel(" ");
        statusLabel.setBorder(BorderFactory.createEmptyBorder(2, 8, 2, 8));
        add(statusLabel, BorderLayout.SOUTH);

        // Update button states on selection change
        sessionList.addListSelectionListener(this::onSelectionChanged);

        // Double-click to attach (running) or resume (exited)
        sessionList.addMouseListener(new MouseAdapter() {
            @Override
            public void mouseClicked(MouseEvent e) {
                if (e.getClickCount() == 2) {
                    ApiModels.SessionResponse s = sessionList.getSelectedValue();
                    if (s == null) return;
                    if ("running".equals(s.status)) {
                        attachSession(s.name);
                    } else if (isResumable(s)) {
                        resumeSession(s.id, s.name);
                    }
                }
            }

            @Override
            public void mousePressed(MouseEvent e) { showPopup(e); }
            @Override
            public void mouseReleased(MouseEvent e) { showPopup(e); }

            private void showPopup(MouseEvent e) {
                if (!e.isPopupTrigger()) return;
                int index = sessionList.locationToIndex(e.getPoint());
                if (index < 0) return;
                sessionList.setSelectedIndex(index);
                ApiModels.SessionResponse session = listModel.get(index);
                createContextMenu(session).show(sessionList, e.getX(), e.getY());
            }
        });

        // Initial button state (nothing selected)
        updateButtonStates(null);

        // Auto-refresh
        refreshAlarm = new Alarm(Alarm.ThreadToUse.POOLED_THREAD, project);
        scheduleRefresh();
        refresh();

        // Auto-resume tracked sessions from previous IDE session (3s delay)
        Alarm resumeAlarm = new Alarm(Alarm.ThreadToUse.POOLED_THREAD, project);
        resumeAlarm.addRequest(() -> autoResumeTrackedSessions(), 3000);
    }

    private JButton createToolbarButton(String text, Icon icon, String tooltip) {
        JButton btn = text != null ? new JButton(text, icon) : new JButton(icon);
        btn.setToolTipText(tooltip);
        btn.setFocusable(false);
        btn.setMargin(new Insets(2, 6, 2, 6));
        return btn;
    }

    private void onSelectionChanged(ListSelectionEvent e) {
        if (e.getValueIsAdjusting()) return;
        updateButtonStates(sessionList.getSelectedValue());
    }

    private void updateButtonStates(ApiModels.SessionResponse s) {
        boolean running = s != null && "running".equals(s.status);
        boolean stopping = s != null && "stopping".equals(s.status);
        boolean alive = running || stopping;
        boolean resumable = s != null && isResumable(s);
        boolean exited = s != null && "exited".equals(s.status);
        boolean hasWorktree = s != null && s.worktree != null;

        attachBtn.setEnabled(running && !attachedSessions.contains(s.name));
        resumeBtn.setEnabled(resumable);
        stopBtn.setEnabled(alive);
        killBtn.setEnabled(alive);
        dismissBtn.setEnabled(exited);

        diffBtn.setEnabled(hasWorktree);
        mergeBtn.setEnabled(hasWorktree && !alive);
        finalizeBtn.setEnabled(hasWorktree && running);
    }

    private static boolean isResumable(ApiModels.SessionResponse s) {
        return "exited".equals(s.status)
                && (s.resume_id != null || s.worktree != null);
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
                List<ApiModels.SessionResponse> sessions = client.listSessions();
                SwingUtilities.invokeLater(() -> {
                    ApiModels.SessionResponse prev = sessionList.getSelectedValue();
                    listModel.clear();
                    int selectIndex = -1;
                    for (int i = 0; i < sessions.size(); i++) {
                        ApiModels.SessionResponse s = sessions.get(i);
                        listModel.addElement(s);
                        if (prev != null && s.id.equals(prev.id)) selectIndex = i;
                    }
                    if (selectIndex >= 0) sessionList.setSelectedIndex(selectIndex);
                    statusLabel.setText(sessions.size() + " session(s)");
                    // Clean up attached-session tracking for sessions that are no longer running
                    Set<String> running = new HashSet<>();
                    for (ApiModels.SessionResponse s : sessions) {
                        if ("running".equals(s.status)) running.add(s.name);
                    }
                    attachedSessions.retainAll(running);
                    updateButtonStates(sessionList.getSelectedValue());
                });
            } catch (Exception e) {
                SwingUtilities.invokeLater(() -> {
                    listModel.clear();
                    statusLabel.setText("Server offline");
                    updateButtonStates(null);
                });
            }
        });
    }

    // === Context menu (right-click) ===

    private JPopupMenu createContextMenu(ApiModels.SessionResponse session) {
        JPopupMenu menu = new JPopupMenu();
        boolean alive = "running".equals(session.status) || "stopping".equals(session.status);

        if (alive) {
            if ("running".equals(session.status)) {
                JMenuItem attachItem = new JMenuItem("Attach");
                attachItem.addActionListener(e -> attachSession(session.name));
                menu.add(attachItem);
                menu.addSeparator();

                JMenuItem stopItem = new JMenuItem("Stop (resume)");
                stopItem.addActionListener(e -> stopSession(session.id, "graceful"));
                menu.add(stopItem);
            }

            JMenuItem killItem = new JMenuItem("Kill");
            killItem.addActionListener(e -> stopSession(session.id, "kill"));
            menu.add(killItem);
        } else {
            if (isResumable(session)) {
                JMenuItem resumeItem = new JMenuItem("Resume");
                resumeItem.addActionListener(e -> resumeSession(session.id, session.name));
                menu.add(resumeItem);
                menu.addSeparator();
            }

            JMenuItem dismissItem = new JMenuItem("Dismiss");
            dismissItem.addActionListener(e -> dismissSession(session.id));
            menu.add(dismissItem);
        }

        // Worktree actions in context menu too
        if (session.worktree != null) {
            menu.addSeparator();

            JMenuItem diffItem = new JMenuItem("View Diff");
            diffItem.addActionListener(e -> viewDiff(session.name));
            menu.add(diffItem);

            if (!alive) {
                JMenuItem mergeItem = new JMenuItem("Merge...");
                mergeItem.addActionListener(e -> mergeWorktree(session));
                menu.add(mergeItem);
            }

            if ("running".equals(session.status)) {
                JMenuItem finalizeItem = new JMenuItem("Finalize");
                finalizeItem.addActionListener(e -> finalizeWorktree(session.name));
                menu.add(finalizeItem);
            }
        }

        return menu;
    }

    // === Session actions ===

    private void attachSession(String name) {
        if (attachedSessions.contains(name)) return;
        attachedSessions.add(name);
        trackSession(project, name);

        String workingDir = project.getBasePath();
        if (workingDir == null) workingDir = System.getProperty("user.home");

        TerminalToolWindowManager manager =
                TerminalToolWindowManager.getInstance(project);
        ShellTerminalWidget widget =
                manager.createLocalShellWidget(workingDir, name);

        try {
            widget.executeCommand("be-conductor attach \"" + name + "\" && exit");
        } catch (java.io.IOException ex) {
            LOG.warn("be-conductor: failed to attach in terminal", ex);
        }
        updateButtonStates(sessionList.getSelectedValue());
    }

    /** Called externally (e.g. from RunSessionAction) to mark a session as attached. */
    public static void markAttached(String name) {
        attachedSessions.add(name);
    }

    private void stopSession(String id, String mode) {
        ApplicationManager.getApplication().executeOnPooledThread(() -> {
            try {
                BeConductorClient.getInstance().stopSession(id, mode);
                // Poll until session transitions out of "stopping" (up to 15s)
                // The graceful stop sequence takes a few seconds for agents
                // to print their resume token.
                for (int i = 0; i < 15; i++) {
                    Thread.sleep(1000);
                    refresh();
                    // Check if session has transitioned
                    List<ApiModels.SessionResponse> sessions =
                            BeConductorClient.getInstance().listSessions();
                    boolean stillStopping = false;
                    for (ApiModels.SessionResponse s : sessions) {
                        if (s.id.equals(id) && "stopping".equals(s.status)) {
                            stillStopping = true;
                            break;
                        }
                    }
                    if (!stillStopping) break;
                }
                refresh();
            } catch (Exception e) {
                SwingUtilities.invokeLater(() ->
                        Notifications.Bus.notify(new Notification(
                                "be-conductor", "Stop Failed", e.getMessage(),
                                NotificationType.ERROR
                        ))
                );
            }
        });
    }

    private void resumeSession(String id, String name) {
        ApplicationManager.getApplication().executeOnPooledThread(() -> {
            try {
                BeConductorClient.getInstance().resumeSession(id);
                SwingUtilities.invokeLater(() -> {
                    attachSession(name);
                    refresh();
                });
            } catch (Exception e) {
                SwingUtilities.invokeLater(() ->
                        Notifications.Bus.notify(new Notification(
                                "be-conductor", "Resume Failed", e.getMessage(),
                                NotificationType.ERROR
                        ))
                );
            }
        });
    }

    private void dismissSession(String id) {
        ApplicationManager.getApplication().executeOnPooledThread(() -> {
            try {
                // Find session name for untracking before deleting
                List<ApiModels.SessionResponse> sessions = BeConductorClient.getInstance().listSessions();
                for (ApiModels.SessionResponse s : sessions) {
                    if (s.id.equals(id)) {
                        untrackSession(project, s.name);
                        break;
                    }
                }
                BeConductorClient.getInstance().deleteSession(id);
                refresh();
            } catch (Exception e) {
                SwingUtilities.invokeLater(() ->
                        Notifications.Bus.notify(new Notification(
                                "be-conductor", "Dismiss Failed", e.getMessage(),
                                NotificationType.ERROR
                        ))
                );
            }
        });
    }

    // === Worktree actions (accessible from session view) ===

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

    private void mergeWorktree(ApiModels.SessionResponse session) {
        String name = session.name;
        ApplicationManager.getApplication().executeOnPooledThread(() -> {
            try {
                ApiModels.WorktreeInfo wt = BeConductorClient.getInstance().getWorktree(name);
                ApiModels.MergePreview preview = BeConductorClient.getInstance().previewMerge(name);
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
                        executeMerge(name, dialog.getStrategy(), dialog.getCommitMessage());
                    }
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

    // === Auto-resume tracked sessions from previous IDE session ===

    private void autoResumeTrackedSessions() {
        List<String> tracked = new java.util.ArrayList<>(getTrackedSessions(project));
        if (tracked.isEmpty()) return;

        try {
            BeConductorClient client = BeConductorClient.getInstance();
            if (!client.isServerRunning()) return;

            List<ApiModels.SessionResponse> sessions = client.listSessions();
            java.util.Map<String, ApiModels.SessionResponse> byName = new java.util.HashMap<>();
            for (ApiModels.SessionResponse s : sessions) byName.put(s.name, s);

            List<String> resumed = new java.util.ArrayList<>();
            List<String> reattached = new java.util.ArrayList<>();

            for (String name : tracked) {
                ApiModels.SessionResponse s = byName.get(name);
                if (s == null) {
                    // Session gone — drop from tracking
                    untrackSession(project, name);
                    continue;
                }
                if ("running".equals(s.status)) {
                    // Still running — just re-attach
                    SwingUtilities.invokeLater(() -> attachSession(s.name));
                    reattached.add(name);
                } else if ("exited".equals(s.status) && (s.resume_id != null || s.worktree != null)) {
                    // Resumable — resume and attach
                    try {
                        client.resumeSession(s.id);
                        SwingUtilities.invokeLater(() -> attachSession(s.name));
                        resumed.add(name);
                    } catch (Exception e) {
                        untrackSession(project, name);
                    }
                } else {
                    // Completed without resume — drop
                    untrackSession(project, name);
                }
            }

            if (!resumed.isEmpty() || !reattached.isEmpty()) {
                StringBuilder msg = new StringBuilder();
                if (!resumed.isEmpty()) msg.append("Resumed ").append(String.join(", ", resumed));
                if (!reattached.isEmpty()) {
                    if (msg.length() > 0) msg.append("; ");
                    msg.append("Re-attached ").append(String.join(", ", reattached));
                }
                String text = msg.toString();
                SwingUtilities.invokeLater(() -> {
                    Notifications.Bus.notify(new Notification(
                            "be-conductor", "Sessions Restored", text,
                            NotificationType.INFORMATION
                    ));
                    refresh();
                });
            }
        } catch (Exception e) {
            // Server not reachable — skip silently
            LOG.info("be-conductor: auto-resume skipped (server unreachable)");
        }
    }

    // === Cell renderer ===

    private static class SessionCellRenderer extends DefaultListCellRenderer {
        @Override
        public Component getListCellRendererComponent(JList<?> list, Object value, int index,
                                                       boolean isSelected, boolean cellHasFocus) {
            if (!(value instanceof ApiModels.SessionResponse session)) {
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
            boolean resumable = isResumable(session);
            if ("running".equals(session.status)) {
                component.setIcon(AllIcons.RunConfigurations.TestPassed);
            } else if ("stopping".equals(session.status)) {
                component.setIcon(AllIcons.Actions.Suspend);
            } else if (resumable) {
                component.setIcon(AllIcons.Actions.Restart);
            } else {
                component.setIcon(AllIcons.RunConfigurations.TestIgnored);
            }

            // Name + command
            component.append(session.name, SimpleTextAttributes.REGULAR_BOLD_ATTRIBUTES);
            component.append("  " + session.command, SimpleTextAttributes.GRAYED_ATTRIBUTES);

            // Worktree branch + commits ahead
            if (session.worktree != null) {
                Object branch = session.worktree.get("branch");
                if (branch != null) {
                    String branchStr = branch.toString();
                    if (branchStr.startsWith("be-conductor/")) {
                        branchStr = branchStr.substring("be-conductor/".length());
                    }
                    component.append("  \u2387 " + branchStr, SimpleTextAttributes.GRAYED_ITALIC_ATTRIBUTES);
                }
                Object ahead = session.worktree.get("commits_ahead");
                if (ahead != null) {
                    int n = ahead instanceof Number ? ((Number) ahead).intValue() : 0;
                    if (n > 0) {
                        component.append(" +" + n, new SimpleTextAttributes(
                                SimpleTextAttributes.STYLE_BOLD, new Color(0x44, 0xbb, 0x77)));
                    }
                }
            }

            // Status indicator
            if ("stopping".equals(session.status)) {
                component.append("  [stopping]", new SimpleTextAttributes(
                        SimpleTextAttributes.STYLE_ITALIC, new Color(0xdd, 0xaa, 0x33)));
            } else if (resumable) {
                component.append("  [resumable]", new SimpleTextAttributes(
                        SimpleTextAttributes.STYLE_ITALIC, new Color(0x44, 0xdd, 0x77)));
            }

            return component;
        }
    }
}
