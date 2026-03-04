package com.somniacs.beconductor;

import com.intellij.notification.Notification;
import com.intellij.notification.NotificationType;
import com.intellij.notification.Notifications;
import com.intellij.openapi.actionSystem.AnAction;
import com.intellij.openapi.actionSystem.AnActionEvent;
import com.intellij.openapi.application.ApplicationManager;
import com.intellij.openapi.diagnostic.Logger;
import com.intellij.openapi.progress.ProgressIndicator;
import com.intellij.openapi.progress.ProgressManager;
import com.intellij.openapi.progress.Task;
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

        String workingDir = cwd;
        if (workingDir == null || workingDir.isEmpty()) {
            workingDir = project.getBasePath();
            if (workingDir == null) workingDir = System.getProperty("user.home");
        }
        final String finalWorkingDir = workingDir;

        // Try API-based creation first, fall back to terminal-only
        ProgressManager.getInstance().run(new Task.Backgroundable(project, "Starting session...") {
            @Override
            public void run(@NotNull ProgressIndicator indicator) {
                try {
                    BeConductorClient client = BeConductorClient.getInstance();
                    // Use generous initial PTY size — the attach command will resize
                    // to the actual terminal dimensions immediately on connect.
                    ApiModels.RunRequest req = new ApiModels.RunRequest(name, command, cwd, worktree, 50, 100);
                    ApiModels.SessionResponse session = client.createSession(req);

                    // Session created via API — open terminal to attach to it
                    SessionListPanel.markAttached(session.name);
                    ApplicationManager.getApplication().invokeLater(() -> {
                        attachInTerminal(project, session.name, finalWorkingDir);
                        BeConductorToolWindowFactory.refreshAll(project);
                    });
                } catch (Exception ex) {
                    LOG.info("API session creation failed, falling back to terminal: " + ex.getMessage());
                    ApplicationManager.getApplication().invokeLater(() ->
                            runInTerminal(project, command, name, finalWorkingDir, worktree)
                    );
                }
            }
        });
    }

    /**
     * Attach to an existing session in a terminal tab.
     */
    private void attachInTerminal(Project project, String sessionName, String workingDir) {
        String tabTitle = sessionName;

        TerminalToolWindowManager manager =
                TerminalToolWindowManager.getInstance(project);
        ShellTerminalWidget widget =
                manager.createLocalShellWidget(workingDir, tabTitle);

        try {
            widget.executeCommand("be-conductor attach " + sessionName + " ; exit");
        } catch (IOException ex) {
            LOG.warn("be-conductor: failed to attach in terminal", ex);
        }
    }

    /**
     * Fallback: create + attach in one terminal command (when API is unavailable).
     */
    private void runInTerminal(Project project, String command, String name, String cwd, boolean worktree) {
        StringBuilder cmd = new StringBuilder("be-conductor run ");
        if (worktree) cmd.append("-w ");
        cmd.append(command).append(" ").append(name);

        String tabTitle = name;

        TerminalToolWindowManager manager =
                TerminalToolWindowManager.getInstance(project);
        ShellTerminalWidget widget =
                manager.createLocalShellWidget(cwd, tabTitle);

        try {
            widget.executeCommand(cmd.toString() + " ; exit");
        } catch (IOException ex) {
            LOG.warn("be-conductor: failed to execute command in terminal", ex);
        }
    }

    @Override
    public void update(@NotNull AnActionEvent e) {
        e.getPresentation().setEnabled(e.getProject() != null);
    }
}
