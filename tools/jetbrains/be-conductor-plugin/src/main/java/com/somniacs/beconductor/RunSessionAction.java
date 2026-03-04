package com.somniacs.beconductor;

import com.intellij.openapi.actionSystem.AnAction;
import com.intellij.openapi.actionSystem.AnActionEvent;
import com.intellij.openapi.diagnostic.Logger;
import com.intellij.openapi.project.Project;
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

        // Run session in terminal (handles server startup, creation, and attach)
        runInTerminal(project, command, name, finalWorkingDir, worktree);
        SessionListPanel.markAttached(name);
        SessionListPanel.trackSession(project, name);
        BeConductorToolWindowFactory.refreshAll(project);
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
