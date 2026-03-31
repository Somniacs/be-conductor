package com.somniacs.beconductor;

import com.intellij.ide.BrowserUtil;
import com.intellij.openapi.actionSystem.AnAction;
import com.intellij.openapi.actionSystem.AnActionEvent;
import com.somniacs.beconductor.api.ServerRegistry;
import org.jetbrains.annotations.NotNull;

public class OpenDashboardAction extends AnAction {

    @Override
    public void actionPerformed(@NotNull AnActionEvent e) {
        // Always open the local dashboard (it handles multi-server itself)
        BrowserUtil.browse(ServerRegistry.getInstance().getBaseUrl("local"));
    }
}
