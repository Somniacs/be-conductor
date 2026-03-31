package com.somniacs.beconductor;

import com.intellij.notification.Notification;
import com.intellij.notification.NotificationType;
import com.intellij.notification.Notifications;
import com.intellij.openapi.actionSystem.AnAction;
import com.intellij.openapi.actionSystem.AnActionEvent;
import com.intellij.openapi.application.ApplicationManager;
import com.intellij.openapi.diagnostic.Logger;
import com.intellij.openapi.project.Project;
import com.somniacs.beconductor.api.ApiModels;
import com.somniacs.beconductor.api.BeConductorClient;
import com.somniacs.beconductor.toolwindow.BeConductorToolWindowFactory;
import com.somniacs.beconductor.toolwindow.SessionListPanel;
import org.jetbrains.annotations.NotNull;
import org.jetbrains.plugins.terminal.ShellTerminalWidget;
import org.jetbrains.plugins.terminal.TerminalToolWindowManager;

import java.io.IOException;

public class RunSessionAction extends AnAction {

    private static final Logger LOG = Logger.getInstance(RunSessionAction.class);

    @Override
    public void actionPerformed(@NotNull AnActionEvent e) {
        Project project = e.getProject();
        if (project == null) return;

        NewSessionDialog dialog = new NewSessionDialog(project);
        if (!dialog.showAndGet()) return;

        String command = dialog.getCommand();
        String name = dialog.getSessionName();
        String cwd = dialog.getWorkingDirectory();
        boolean worktree = dialog.isWorktreeEnabled();
        String sessionType = dialog.getSessionType();
        String serverKey = dialog.getServerKey();

        String workingDir = cwd;
        if (workingDir == null || workingDir.isEmpty()) {
            workingDir = project.getBasePath();
            if (workingDir == null) workingDir = System.getProperty("user.home");
        }
        final String finalWorkingDir = workingDir;

        if ("agent".equals(sessionType)) {
            // Agent sessions: create via API and open in native panel
            createAgentSession(project, serverKey, command, name, finalWorkingDir, worktree);
        } else {
            // PTY sessions: run in terminal (handles server startup, creation, and attach)
            runInTerminal(project, command, name, finalWorkingDir, worktree);
            SessionListPanel.markAttached(name);
        }
        SessionListPanel.trackSession(project, name);
        BeConductorToolWindowFactory.refreshAll(project);
    }

    /**
     * Create an agent session via the REST API and open it in a native panel.
     */
    private void createAgentSession(Project project, String serverKey, String command, String name, String cwd, boolean worktree) {
        ApplicationManager.getApplication().executeOnPooledThread(() -> {
            try {
                BeConductorClient client = BeConductorClient.getInstance();
                ApiModels.RunRequest request = new ApiModels.RunRequest(name, command, cwd, worktree, "agent");
                ApiModels.SessionResponse session = client.createSession(serverKey, request);
                String sessionId = session != null ? session.id : name;
                javax.swing.SwingUtilities.invokeLater(() -> {
                    BeConductorToolWindowFactory.refreshAll(project);
                    SessionListPanel.openAgentSession(project, serverKey, sessionId);
                });
            } catch (Exception ex) {
                LOG.warn("be-conductor: failed to create agent session", ex);
                javax.swing.SwingUtilities.invokeLater(() ->
                        Notifications.Bus.notify(new Notification(
                                "be-conductor", "Agent Session Failed", ex.getMessage() != null ? ex.getMessage() : ex.toString(),
                                NotificationType.ERROR
                        ))
                );
            }
        });
    }

    /**
     * Run session in terminal (handles server startup, creation, and attach).
     */
    private void runInTerminal(Project project, String command, String name, String cwd, boolean worktree) {
        StringBuilder cmd = new StringBuilder("be-conductor run ");
        if (worktree) cmd.append("-w ");
        cmd.append("\"").append(command).append("\" \"").append(name).append("\"");

        String tabTitle = name;

        TerminalToolWindowManager manager =
                TerminalToolWindowManager.getInstance(project);
        ShellTerminalWidget widget =
                manager.createLocalShellWidget(cwd, tabTitle);

        try {
            widget.executeCommand(cmd.toString() + " && exit");
        } catch (IOException ex) {
            LOG.warn("be-conductor: failed to execute command in terminal", ex);
        }
    }

    @Override
    public void update(@NotNull AnActionEvent e) {
        e.getPresentation().setEnabled(e.getProject() != null);
    }
}
