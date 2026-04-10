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
import com.somniacs.beconductor.api.ServerRegistry;
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

    private static final String RUNNING_AT_CLOSE_KEY = "be-conductor.runningAtClose";

    public static void setRunningAtClose(Project project, List<String> names) {
        PropertiesComponent props = PropertiesComponent.getInstance(project);
        if (names.isEmpty()) {
            props.unsetValue(RUNNING_AT_CLOSE_KEY);
        } else {
            props.setValue(RUNNING_AT_CLOSE_KEY, String.join(",", names));
        }
    }

    public static List<String> getRunningAtClose(Project project) {
        PropertiesComponent props = PropertiesComponent.getInstance(project);
        String val = props.getValue(RUNNING_AT_CLOSE_KEY);
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
    /** Header item for server group separators in the list. */
    record ServerHeader(String serverKey, String label, int sessionCount, boolean online) {}

    private final DefaultListModel<Object> listModel;  // ServerHeader | SessionResponse
    private final JBList<Object> sessionList;
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

    /** Get the selected session, or null if a header or nothing is selected. */
    private ApiModels.SessionResponse getSelectedSession() {
        Object val = sessionList.getSelectedValue();
        return val instanceof ApiModels.SessionResponse s ? s : null;
    }

    public SessionListPanel(Project project) {
        super(new BorderLayout());
        this.project = project;

        // === List (initialize early so toolbar lambdas can reference it) ===
        listModel = new DefaultListModel<>();
        sessionList = new JBList<>(listModel);
        sessionList.setCellRenderer(new MixedCellRenderer());
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
                "Open terminal attached to this session (or dashboard for SDK sessions)");
        attachBtn.addActionListener(e -> {
            ApiModels.SessionResponse s = getSelectedSession();
            if (s == null) return;
            if (s.isAgent()) {
                openAgentInBrowser(s.serverKey, s.id, s.name);
            } else {
                attachSession(s.name, s.cwd);
            }
        });
        sessionActions.add(attachBtn);

        resumeBtn = createToolbarButton("Resume", AllIcons.Actions.Execute,
                "Resume this session (double-click also works)");
        resumeBtn.addActionListener(e -> {
            ApiModels.SessionResponse s = getSelectedSession();
            if (s != null) resumeSession(s.serverKey, s.id, s.name, s.cwd, s.isAgent());
        });
        sessionActions.add(resumeBtn);

        stopBtn = createToolbarButton("Stop", AllIcons.Actions.Suspend,
                "Gracefully stop (session stays resumable)");
        stopBtn.addActionListener(e -> {
            ApiModels.SessionResponse s = getSelectedSession();
            if (s == null) return;
            String mode = "stopping".equals(s.status) ? "kill" : "graceful";
            stopSession(s.serverKey, s.id, mode);
        });
        sessionActions.add(stopBtn);

        killBtn = createToolbarButton("Kill", AllIcons.Actions.Cancel,
                "Force stop and remove session");
        killBtn.addActionListener(e -> {
            ApiModels.SessionResponse s = getSelectedSession();
            if (s != null) stopSession(s.serverKey, s.id, "kill");
        });
        sessionActions.add(killBtn);

        dismissBtn = createToolbarButton("Dismiss", AllIcons.Actions.GC,
                "Remove this exited session");
        dismissBtn.addActionListener(e -> {
            ApiModels.SessionResponse s = getSelectedSession();
            if (s != null) dismissSession(s.serverKey, s.id);
        });
        sessionActions.add(dismissBtn);

        toolbar.add(sessionActions);

        // Row 2: Worktree actions
        JPanel worktreeActions = new JPanel(new FlowLayout(FlowLayout.LEFT, 2, 1));

        diffBtn = createToolbarButton("Diff", AllIcons.Actions.Diff,
                "View worktree diff");
        diffBtn.addActionListener(e -> {
            ApiModels.SessionResponse s = getSelectedSession();
            if (s != null && s.worktree != null) viewDiff(s.serverKey, s.name);
        });
        worktreeActions.add(diffBtn);

        mergeBtn = createToolbarButton("Merge", AllIcons.Vcs.Merge,
                "Merge worktree into base branch");
        mergeBtn.addActionListener(e -> {
            ApiModels.SessionResponse s = getSelectedSession();
            if (s != null && s.worktree != null) mergeWorktree(s);
        });
        worktreeActions.add(mergeBtn);

        finalizeBtn = createToolbarButton("Finalize", AllIcons.Actions.Commit,
                "Finalize worktree (auto-commit and mark done)");
        finalizeBtn.addActionListener(e -> {
            ApiModels.SessionResponse s = getSelectedSession();
            if (s != null && s.worktree != null) finalizeWorktree(s.serverKey, s.name);
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
                    ApiModels.SessionResponse s = getSelectedSession();
                    if (s == null) return;
                    if ("running".equals(s.status)) {
                        if (s.isAgent()) {
                            openAgentInBrowser(s.serverKey, s.id, s.name);
                        } else {
                            attachSession(s.name, s.cwd);
                        }
                    } else if (isResumable(s)) {
                        resumeSession(s.serverKey, s.id, s.name, s.cwd, s.isAgent());
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
                Object item = listModel.get(index);
                if (!(item instanceof ApiModels.SessionResponse session)) return;
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
        updateButtonStates(getSelectedSession());
    }

    private void updateButtonStates(ApiModels.SessionResponse s) {
        boolean running = s != null && "running".equals(s.status);
        boolean stopping = s != null && "stopping".equals(s.status);
        boolean alive = running || stopping;
        boolean resumable = s != null && isResumable(s);
        boolean exited = s != null && "exited".equals(s.status);
        boolean hasWorktree = s != null && s.worktree != null;
        boolean isAgent = s != null && s.isAgent();

        // Agent sessions can always "attach" (opens browser), PTY sessions check terminal tracking
        attachBtn.setEnabled(running && (isAgent || !attachedSessions.contains(s.name)));
        attachBtn.setText(isAgent ? "Open" : "Attach");
        attachBtn.setToolTipText(isAgent ? "Open agent session in editor tab" : "Open terminal attached to this session");
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
                var registry = com.somniacs.beconductor.api.ServerRegistry.getInstance();
                var enabledServers = registry.getEnabledServers();
                // Fetch sessions from all enabled servers in parallel
                java.util.List<ApiModels.SessionResponse> allSessions = java.util.Collections.synchronizedList(new java.util.ArrayList<>());
                java.util.concurrent.CountDownLatch latch = new java.util.concurrent.CountDownLatch(enabledServers.size());
                for (var server : enabledServers) {
                    ApplicationManager.getApplication().executeOnPooledThread(() -> {
                        try {
                            List<ApiModels.SessionResponse> sessions = client.listSessions(server.key);
                            for (var s : sessions) s.serverKey = server.key;
                            allSessions.addAll(sessions);
                        } catch (Exception ignored) {
                            // Server offline — skip
                        } finally {
                            latch.countDown();
                        }
                    });
                }
                latch.await(8, java.util.concurrent.TimeUnit.SECONDS);
                final boolean multiServer = registry.isMultiServer();
                SwingUtilities.invokeLater(() -> {
                    ApiModels.SessionResponse prev = getSelectedSession();
                    String prevCompound = prev != null ? prev.compoundId() : null;
                    listModel.clear();
                    int selectIndex = -1;

                    if (multiServer) {
                        // Group by server: local first, then others sorted by label
                        java.util.Map<String, java.util.List<ApiModels.SessionResponse>> byServer = new java.util.LinkedHashMap<>();
                        // Ensure local is first
                        for (var srv : enabledServers) byServer.put(srv.key, new java.util.ArrayList<>());
                        for (var s : allSessions) {
                            byServer.computeIfAbsent(s.serverKey != null ? s.serverKey : "local", k -> new java.util.ArrayList<>()).add(s);
                        }
                        for (var entry : byServer.entrySet()) {
                            String serverKey = entry.getKey();
                            var sessions = entry.getValue();
                            ServerRegistry.Server srv = registry.getServer(serverKey);
                            String label = srv != null ? srv.label : serverKey;
                            listModel.addElement(new ServerHeader(serverKey, label, sessions.size(), !sessions.isEmpty()));
                            for (var s : sessions) {
                                listModel.addElement(s);
                                if (prevCompound != null && s.compoundId().equals(prevCompound)) {
                                    selectIndex = listModel.size() - 1;
                                }
                            }
                        }
                    } else {
                        for (int i = 0; i < allSessions.size(); i++) {
                            ApiModels.SessionResponse s = allSessions.get(i);
                            listModel.addElement(s);
                            if (prevCompound != null && s.compoundId().equals(prevCompound)) selectIndex = i;
                        }
                    }
                    if (selectIndex >= 0) sessionList.setSelectedIndex(selectIndex);
                    statusLabel.setText(allSessions.size() + " session(s)");
                    Set<String> running = new HashSet<>();
                    for (ApiModels.SessionResponse s : allSessions) {
                        if ("running".equals(s.status)) running.add(s.name);
                    }
                    attachedSessions.retainAll(running);
                    updateButtonStates(getSelectedSession());
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
                if (session.isAgent()) {
                    JMenuItem openItem = new JMenuItem("Open in Editor");
                    openItem.addActionListener(e -> openAgentInBrowser(session.serverKey, session.id, session.name));
                    menu.add(openItem);

                    JMenuItem panelItem = new JMenuItem("Open as Panel");
                    panelItem.addActionListener(e -> openAgentAsToolWindow(session.serverKey, session.id, session.name));
                    menu.add(panelItem);
                } else {
                    JMenuItem attachItem = new JMenuItem("Attach");
                    attachItem.addActionListener(e -> attachSession(session.name, session.cwd));
                    menu.add(attachItem);
                }

                JMenuItem cloneItem = new JMenuItem("Clone");
                cloneItem.addActionListener(e -> cloneSession(session));
                menu.add(cloneItem);
                menu.addSeparator();

                JMenuItem stopItem = new JMenuItem("Stop (resume)");
                stopItem.addActionListener(e -> stopSession(session.serverKey, session.id, "graceful"));
                menu.add(stopItem);
            }

            JMenuItem killItem = new JMenuItem("Kill");
            killItem.addActionListener(e -> stopSession(session.serverKey, session.id, "kill"));
            menu.add(killItem);
        } else {
            if (isResumable(session)) {
                JMenuItem resumeItem = new JMenuItem("Resume");
                resumeItem.addActionListener(e -> resumeSession(session.serverKey, session.id, session.name, session.cwd, session.isAgent()));
                menu.add(resumeItem);
                menu.addSeparator();
            }

            JMenuItem dismissItem = new JMenuItem("Dismiss");
            dismissItem.addActionListener(e -> dismissSession(session.serverKey, session.id));
            menu.add(dismissItem);
        }

        // Worktree actions in context menu too
        if (session.worktree != null) {
            menu.addSeparator();

            JMenuItem diffItem = new JMenuItem("View Diff");
            diffItem.addActionListener(e -> viewDiff(session.serverKey, session.name));
            menu.add(diffItem);

            if (!alive) {
                JMenuItem mergeItem = new JMenuItem("Merge...");
                mergeItem.addActionListener(e -> mergeWorktree(session));
                menu.add(mergeItem);
            }

            if ("running".equals(session.status)) {
                JMenuItem finalizeItem = new JMenuItem("Finalize");
                finalizeItem.addActionListener(e -> finalizeWorktree(session.serverKey, session.name));
                menu.add(finalizeItem);
            }
        }

        return menu;
    }

    // === Session actions ===

    private void attachSession(String name) {
        attachSession(name, null);
    }

    private void attachSession(String name, String cwd) {
        if (attachedSessions.contains(name)) return;

        // Warn if already attached elsewhere
        try {
            List<ApiModels.SessionResponse> allSessions = BeConductorClient.getInstance().listSessions("local");
            for (ApiModels.SessionResponse s : allSessions) {
                if (s.name.equals(name) && s.attached_clients != null && !s.attached_clients.isEmpty()) {
                    Set<String> sources = new java.util.LinkedHashSet<>();
                    for (ApiModels.AttachedClient c : s.attached_clients) {
                        if (c.source != null) sources.add(c.source);
                    }
                    int result = JOptionPane.showConfirmDialog(
                            this,
                            "\"" + name + "\" is already attached in: " + String.join(", ", sources)
                                    + ".\n\nOpen here as well?",
                            "Session Already Attached",
                            JOptionPane.YES_NO_OPTION,
                            JOptionPane.WARNING_MESSAGE
                    );
                    if (result != JOptionPane.YES_OPTION) return;
                    break;
                }
            }
        } catch (Exception ignored) {
            // If check fails, proceed anyway
        }

        attachedSessions.add(name);
        trackSession(project, name);

        String workingDir = cwd;
        if (workingDir == null || workingDir.isEmpty()) {
            workingDir = project.getBasePath();
        }
        if (workingDir == null) workingDir = System.getProperty("user.home");

        TerminalToolWindowManager manager =
                TerminalToolWindowManager.getInstance(project);
        ShellTerminalWidget widget =
                manager.createLocalShellWidget(workingDir, name);

        try {
            widget.executeCommand(com.somniacs.beconductor.TerminalCommandUtil.exitOnSuccess("be-conductor attach \"" + name + "\""));
        } catch (java.io.IOException ex) {
            LOG.warn("be-conductor: failed to attach in terminal", ex);
        }
        updateButtonStates(getSelectedSession());
    }

    /** Called externally (e.g. from RunSessionAction) to mark a session as attached. */
    public static void markAttached(String name) {
        attachedSessions.add(name);
    }

    /** Open an agent session in a native panel inside the IDE (static entry point). */
    public static void openAgentSession(com.intellij.openapi.project.Project proj, String serverKey, String sessionId) {
        openAgentSession(proj, serverKey, sessionId, null);
    }

    public static void openAgentSession(com.intellij.openapi.project.Project proj, String serverKey, String sessionId, String sessionName) {
        ApplicationManager.getApplication().invokeLater(() -> {
            if (proj == null || proj.isDisposed()) return;
            openAgentPanel(proj, serverKey, sessionId, sessionName);
        });
    }

    /** Convenience overload for local server. */
    public static void openAgentSession(com.intellij.openapi.project.Project proj, String sessionId) {
        openAgentSession(proj, "local", sessionId);
    }

    /** Open an agent session as a dockable tool window (static entry point). */
    public static void openAgentSessionAsPanel(com.intellij.openapi.project.Project proj, String serverKey, String sessionId) {
        openAgentSessionAsPanel(proj, serverKey, sessionId, null);
    }

    public static void openAgentSessionAsPanel(com.intellij.openapi.project.Project proj, String serverKey, String sessionId, String sessionName) {
        ApplicationManager.getApplication().invokeLater(() -> {
            if (proj == null || proj.isDisposed()) return;
            openAgentToolWindow(proj, serverKey, sessionId, sessionName);
        });
    }

    /** Open an agent session in the editor area (default). */
    private void openAgentInBrowser(String serverKey, String sessionId) {
        openAgentPanel(project, serverKey, sessionId, null);
    }

    private void openAgentInBrowser(String serverKey, String sessionId, String sessionName) {
        openAgentPanel(project, serverKey, sessionId, sessionName);
    }

    /** Open an agent session as a dockable tool window (bottom/side/floating). */
    private void openAgentAsToolWindow(String serverKey, String sessionId) {
        openAgentToolWindow(project, serverKey, sessionId);
    }

    private void openAgentAsToolWindow(String serverKey, String sessionId, String sessionName) {
        openAgentToolWindow(project, serverKey, sessionId, sessionName);
    }

    /**
     * Open (or focus) an agent session as a tool window — can be docked to
     * bottom, left, right, or floated as a separate window.
     */
    private static void openAgentToolWindow(com.intellij.openapi.project.Project proj, String serverKey, String sessionId) {
        openAgentToolWindow(proj, serverKey, sessionId, null);
    }

    private static void openAgentToolWindow(com.intellij.openapi.project.Project proj, String serverKey, String sessionId, String sessionName) {
        String displayName = sessionName != null && !sessionName.isEmpty() ? sessionName : sessionId;
        String twId = "Agent: " + sessionId;
        com.intellij.openapi.wm.ToolWindow existing =
                com.intellij.openapi.wm.ToolWindowManager.getInstance(proj).getToolWindow(twId);
        if (existing != null) {
            existing.show();
            return;
        }
        com.intellij.openapi.wm.ToolWindowManager twm =
                com.intellij.openapi.wm.ToolWindowManager.getInstance(proj);
        com.intellij.openapi.wm.ToolWindow tw = twm.registerToolWindow(
                twId,
                true,
                com.intellij.openapi.wm.ToolWindowAnchor.BOTTOM
        );
        tw.setTitle(displayName);
        tw.setStripeTitle(displayName + " (Agent)");
        tw.setIcon(com.intellij.openapi.util.IconLoader.getIcon("/icons/be-conductor.svg", SessionListPanel.class));

        com.somniacs.beconductor.agent.AgentSessionPanel panel =
                new com.somniacs.beconductor.agent.AgentSessionPanel(proj, serverKey, sessionId);
        com.intellij.ui.content.Content content = tw.getContentManager()
                .getFactory().createContent(panel, displayName, false);
        content.setCloseable(true);
        content.setDisposer(panel);
        tw.getContentManager().addContent(content);
        tw.show();
    }

    /**
     * Open (or focus) an agent session in the editor area — can be docked,
     * split, and placed alongside code files like a normal editor tab.
     */
    private static void openAgentPanel(com.intellij.openapi.project.Project proj, String serverKey, String sessionId) {
        openAgentPanel(proj, serverKey, sessionId, null);
    }

    private static void openAgentPanel(com.intellij.openapi.project.Project proj, String serverKey, String sessionId, String sessionName) {
        ServerRegistry registry = ServerRegistry.getInstance();
        String baseUrl = registry.getBaseUrl(serverKey);
        String wsBase = baseUrl.replaceFirst("^http", "ws");
        String url = baseUrl + "/agent/" + java.net.URLEncoder.encode(sessionId, java.nio.charset.StandardCharsets.UTF_8)
                + "?session=" + java.net.URLEncoder.encode(sessionId, java.nio.charset.StandardCharsets.UTF_8)
                + "&ws=" + java.net.URLEncoder.encode(wsBase, java.nio.charset.StandardCharsets.UTF_8);

        String displayName = sessionName != null && !sessionName.isEmpty() ? sessionName : sessionId;

        // Open as editor tab via LightVirtualFile + custom FileEditorProvider
        try {
            com.intellij.testFramework.LightVirtualFile vf =
                    new com.intellij.testFramework.LightVirtualFile(displayName + " (Agent)");
            vf.putUserData(com.somniacs.beconductor.agent.AgentFileEditorProvider.AGENT_URL_KEY, url);
            vf.putUserData(com.somniacs.beconductor.agent.AgentFileEditorProvider.AGENT_SESSION_KEY, displayName);
            com.intellij.openapi.fileEditor.FileEditorManager.getInstance(proj).openFile(vf, true);
            return;
        } catch (Exception | NoClassDefFoundError e) {
            LOG.info("be-conductor: Custom FileEditor not available, falling back to tool window");
        }

        // Fallback: register a tool window
        String twId = "Agent: " + displayName;
        com.intellij.openapi.wm.ToolWindow existing =
                com.intellij.openapi.wm.ToolWindowManager.getInstance(proj).getToolWindow(twId);
        if (existing != null) {
            existing.show();
            return;
        }
        com.intellij.openapi.wm.ToolWindowManager twm =
                com.intellij.openapi.wm.ToolWindowManager.getInstance(proj);
        com.intellij.openapi.wm.ToolWindow tw = twm.registerToolWindow(
                twId,
                true,
                com.intellij.openapi.wm.ToolWindowAnchor.BOTTOM
        );
        tw.setTitle(displayName);
        tw.setStripeTitle(displayName + " (Agent)");
        try { tw.setIcon(com.intellij.openapi.util.IconLoader.getIcon("/icons/be-conductor.svg", SessionListPanel.class)); } catch (Exception ignored) {}

        com.somniacs.beconductor.agent.AgentSessionPanel panel =
                new com.somniacs.beconductor.agent.AgentSessionPanel(proj, serverKey, sessionId);
        com.intellij.ui.content.Content content = tw.getContentManager()
                .getFactory().createContent(panel, displayName, false);
        content.setCloseable(true);
        content.setDisposer(panel);
        tw.getContentManager().addContent(content);
        tw.show();
    }

    private void stopSession(String serverKey, String id, String mode) {
        ApplicationManager.getApplication().executeOnPooledThread(() -> {
            try {
                BeConductorClient.getInstance().stopSession(serverKey, id, mode);
                // Poll until session transitions out of "stopping" (up to 15s)
                for (int i = 0; i < 15; i++) {
                    Thread.sleep(1000);
                    refresh();
                    List<ApiModels.SessionResponse> sessions =
                            BeConductorClient.getInstance().listSessions(serverKey);
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

    /** Estimate terminal dimensions from the IDE's main frame. */
    private int[] estimateTerminalDimensions() {
        java.awt.Window frame = SwingUtilities.getWindowAncestor(this);
        int cols = 120, rows = 30;
        if (frame != null) {
            // Rough estimate: terminal area ≈ 70% of frame width, 40% of height
            // at ~8px per char and ~16px per row, with a safety margin
            cols = Math.max((int) (frame.getWidth() * 0.7 / 8) - 2, 80);
            rows = Math.max((int) (frame.getHeight() * 0.4 / 16) - 1, 24);
        }
        return new int[]{rows, cols};
    }

    private void resumeSession(String serverKey, String id, String name, String cwd, boolean isAgent) {
        int[] dims = estimateTerminalDimensions();
        ApplicationManager.getApplication().executeOnPooledThread(() -> {
            try {
                BeConductorClient.getInstance().resumeSession(serverKey, id, dims[0], dims[1]);
                SwingUtilities.invokeLater(() -> {
                    if (isAgent) {
                        openAgentInBrowser(serverKey, id, name);
                    } else {
                        attachSession(name, cwd);
                    }
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

    private void dismissSession(String serverKey, String id) {
        ApplicationManager.getApplication().executeOnPooledThread(() -> {
            try {
                // Find session name for untracking before deleting
                List<ApiModels.SessionResponse> sessions = BeConductorClient.getInstance().listSessions(serverKey);
                for (ApiModels.SessionResponse s : sessions) {
                    if (s.id.equals(id)) {
                        untrackSession(project, s.name);
                        break;
                    }
                }
                BeConductorClient.getInstance().deleteSession(serverKey, id);
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

    private void cloneSession(ApiModels.SessionResponse session) {
        SwingUtilities.invokeLater(() -> {
            String name = JOptionPane.showInputDialog(
                    this,
                    "Name for the cloned session:",
                    "Clone Session",
                    JOptionPane.PLAIN_MESSAGE,
                    null, null,
                    session.name + "-clone"
            ) instanceof String s ? s.trim() : null;
            if (name == null || name.isEmpty()) return;

            String cloneName = name;
            ApplicationManager.getApplication().executeOnPooledThread(() -> {
                try {
                    BeConductorClient.getInstance().cloneSession(
                            session.serverKey, session.id, new ApiModels.CloneRequest(cloneName));
                    SwingUtilities.invokeLater(() -> {
                        Notifications.Bus.notify(new Notification(
                                "be-conductor", "Clone Started",
                                "Cloning \"" + session.name + "\" into \"" + cloneName + "\"...",
                                NotificationType.INFORMATION
                        ));
                    });
                    // Poll until the cloned session appears (up to 90s)
                    for (int i = 0; i < 90; i++) {
                        Thread.sleep(1000);
                        List<ApiModels.SessionResponse> sessions =
                                BeConductorClient.getInstance().listSessions(session.serverKey);
                        for (ApiModels.SessionResponse s : sessions) {
                            if (s.name.equals(cloneName) && "running".equals(s.status)) {
                                refresh();
                                return;
                            }
                        }
                    }
                    refresh();
                } catch (Exception e) {
                    SwingUtilities.invokeLater(() ->
                            Notifications.Bus.notify(new Notification(
                                    "be-conductor", "Clone Failed", e.getMessage(),
                                    NotificationType.ERROR
                            ))
                    );
                }
            });
        });
    }

    // === Worktree actions (accessible from session view) ===

    private void viewDiff(String serverKey, String name) {
        ApplicationManager.getApplication().executeOnPooledThread(() -> {
            try {
                ApiModels.RichDiffResponse richDiff =
                        BeConductorClient.getInstance().getWorktreeRichDiff(serverKey, name);
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
                viewDiffFallback(serverKey, name);
            }
        });
    }

    private void viewDiffFallback(String serverKey, String name) {
        try {
            ApiModels.DiffResponse diff = BeConductorClient.getInstance().getWorktreeDiff(serverKey, name);
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
                ApiModels.WorktreeInfo wt = BeConductorClient.getInstance().getWorktree(session.serverKey, name);
                ApiModels.MergePreview preview = BeConductorClient.getInstance().previewMerge(session.serverKey, name);
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
                        executeMerge(session.serverKey, name, dialog.getStrategy(), dialog.getCommitMessage());
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

    private void executeMerge(String serverKey, String name, String strategy, String message) {
        ApplicationManager.getApplication().executeOnPooledThread(() -> {
            try {
                ApiModels.MergeResult result = BeConductorClient.getInstance()
                        .executeMerge(serverKey, name, strategy, message);
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

    private void finalizeWorktree(String serverKey, String name) {
        ApplicationManager.getApplication().executeOnPooledThread(() -> {
            try {
                ApiModels.WorktreeInfo result = BeConductorClient.getInstance().finalizeWorktree(serverKey, name);
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

        // Only resume sessions that were actually running when the IDE closed.
        Set<String> wasRunning = new java.util.HashSet<>(getRunningAtClose(project));
        setRunningAtClose(project, java.util.Collections.emptyList());  // consume once

        // Estimate terminal dimensions from the IDE frame on the EDT
        final int[] dims = {30, 120};  // defaults
        try {
            SwingUtilities.invokeAndWait(() -> {
                int[] est = estimateTerminalDimensions();
                dims[0] = est[0];
                dims[1] = est[1];
            });
        } catch (Exception ignored) {}

        try {
            BeConductorClient client = BeConductorClient.getInstance();
            if (!client.isServerRunning("local")) return;

            List<ApiModels.SessionResponse> sessions = client.listSessions("local");
            java.util.Map<String, ApiModels.SessionResponse> byName = new java.util.HashMap<>();
            for (ApiModels.SessionResponse s : sessions) { s.serverKey = "local"; byName.put(s.name, s); }

            List<String> resumed = new java.util.ArrayList<>();
            List<String> reattached = new java.util.ArrayList<>();

            for (String name : tracked) {
                ApiModels.SessionResponse s = byName.get(name);
                if (s == null) {
                    untrackSession(project, name);
                    continue;
                }
                if ("running".equals(s.status)) {
                    if (s.isAgent()) {
                        SwingUtilities.invokeLater(() -> openAgentInBrowser(s.serverKey, s.id, s.name));
                    } else {
                        SwingUtilities.invokeLater(() -> attachSession(s.name, s.cwd));
                    }
                    reattached.add(name);
                } else if ("exited".equals(s.status) && (s.resume_id != null || s.worktree != null)
                        && wasRunning.contains(name)) {
                    try {
                        ApiModels.SessionResponse resumed_s = client.resumeSession("local", s.id, dims[0], dims[1]);
                        String resumedCwd = resumed_s != null ? resumed_s.cwd : s.cwd;
                        if (s.isAgent()) {
                            SwingUtilities.invokeLater(() -> openAgentInBrowser(s.serverKey, s.id, s.name));
                        } else {
                            SwingUtilities.invokeLater(() -> attachSession(s.name, resumedCwd));
                        }
                        resumed.add(name);
                    } catch (Exception e) {
                        untrackSession(project, name);
                    }
                } else {
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

    private static class MixedCellRenderer extends DefaultListCellRenderer {
        @Override
        public Component getListCellRendererComponent(JList<?> list, Object value, int index,
                                                       boolean isSelected, boolean cellHasFocus) {
            // Server group header
            if (value instanceof ServerHeader header) {
                SimpleColoredComponent component = new SimpleColoredComponent();
                component.setOpaque(true);
                component.setBackground(isSelected ? list.getSelectionBackground() : list.getBackground());
                component.setForeground(isSelected ? list.getSelectionForeground() : list.getForeground());
                component.setIcon(AllIcons.Nodes.HomeFolder);
                component.append(header.label(), new SimpleTextAttributes(
                        SimpleTextAttributes.STYLE_BOLD, new Color(0x88, 0x99, 0xcc)));
                component.append("  (" + header.sessionCount() + ")", SimpleTextAttributes.GRAYED_ATTRIBUTES);
                return component;
            }

            // Session item
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

            // Indent when grouped under a server header
            if (ServerRegistry.getInstance().isMultiServer()) {
                component.setIpad(new Insets(0, 12, 0, 0));
            }

            // Name + session type badge + command
            component.append(session.name, SimpleTextAttributes.REGULAR_BOLD_ATTRIBUTES);
            if (session.isAgent()) {
                component.append("  GUI", new SimpleTextAttributes(
                        SimpleTextAttributes.STYLE_BOLD, new Color(0x4a, 0x6c, 0xf7)));
                component.append(" \u00b7 ", SimpleTextAttributes.GRAYED_ATTRIBUTES);
            } else {
                component.append("  ", SimpleTextAttributes.GRAYED_ATTRIBUTES);
            }
            component.append(session.command, SimpleTextAttributes.GRAYED_ATTRIBUTES);

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
            if ("running".equals(session.status) && attachedSessions.contains(session.name)) {
                component.append("  [attached]", new SimpleTextAttributes(
                        SimpleTextAttributes.STYLE_ITALIC, new Color(0x60, 0xb0, 0xff)));
            } else if ("stopping".equals(session.status)) {
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
