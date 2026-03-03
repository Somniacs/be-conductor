package com.somniacs.beconductor;

import com.intellij.openapi.actionSystem.AnAction;
import com.intellij.openapi.actionSystem.AnActionEvent;
import com.intellij.openapi.diagnostic.Logger;
import com.intellij.openapi.project.Project;
import org.jetbrains.annotations.NotNull;
import org.jetbrains.plugins.terminal.ShellTerminalWidget;
import org.jetbrains.plugins.terminal.TerminalToolWindowManager;

import java.io.IOException;

public class RunSessionAction extends AnAction {

    private static final Logger LOG = Logger.getInstance(RunSessionAction.class);

    @Override
    public void actionPerformed(@NotNull AnActionEvent e) {
        Project project = e.getProject();
        if (project == null) {
            return;
        }

        NewSessionDialog dialog = new NewSessionDialog(project);
        if (!dialog.showAndGet()) {
            return;
        }

        String agent = dialog.getAgent();
        String name = dialog.getSessionName();
        String command = "be-conductor run " + agent + " " + name;

        String workingDir = project.getBasePath();
        if (workingDir == null) {
            workingDir = System.getProperty("user.home");
        }

        String tabTitle = name + " (" + agent + ")";

        TerminalToolWindowManager manager =
            TerminalToolWindowManager.getInstance(project);
        ShellTerminalWidget widget =
            manager.createLocalShellWidget(workingDir, tabTitle);

        try {
            widget.executeCommand(command);
        } catch (IOException ex) {
            LOG.warn("be-conductor: failed to execute command in terminal", ex);
        }
    }

    @Override
    public void update(@NotNull AnActionEvent e) {
        e.getPresentation().setEnabled(e.getProject() != null);
    }
}
