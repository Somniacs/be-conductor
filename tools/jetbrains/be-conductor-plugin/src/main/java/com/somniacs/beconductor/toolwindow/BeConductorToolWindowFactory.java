package com.somniacs.beconductor.toolwindow;

import com.intellij.openapi.project.Project;
import com.intellij.openapi.wm.ToolWindow;
import com.intellij.openapi.wm.ToolWindowFactory;
import com.intellij.openapi.wm.ToolWindowManager;
import com.intellij.ui.content.Content;
import com.intellij.ui.content.ContentFactory;
import org.jetbrains.annotations.NotNull;

/**
 * Factory for the be-conductor tool window (sidebar).
 * Creates two tabs: Sessions and Worktrees.
 */
public class BeConductorToolWindowFactory implements ToolWindowFactory {

    public static final String TOOL_WINDOW_ID = "be-conductor";

    @Override
    public void createToolWindowContent(@NotNull Project project, @NotNull ToolWindow toolWindow) {
        SessionListPanel sessionPanel = new SessionListPanel(project);
        WorktreeListPanel worktreePanel = new WorktreeListPanel(project);

        ContentFactory contentFactory = ContentFactory.getInstance();

        Content sessionsContent = contentFactory.createContent(sessionPanel, "Sessions", false);
        toolWindow.getContentManager().addContent(sessionsContent);

        Content worktreesContent = contentFactory.createContent(worktreePanel, "Worktrees", false);
        toolWindow.getContentManager().addContent(worktreesContent);
    }

    /**
     * Refresh all panels in the be-conductor tool window.
     */
    public static void refreshAll(@NotNull Project project) {
        ToolWindow tw = ToolWindowManager.getInstance(project).getToolWindow(TOOL_WINDOW_ID);
        if (tw == null) return;
        for (Content content : tw.getContentManager().getContents()) {
            if (content.getComponent() instanceof SessionListPanel panel) {
                panel.refresh();
            } else if (content.getComponent() instanceof WorktreeListPanel panel) {
                panel.refresh();
            }
        }
    }
}
