package com.somniacs.beconductor;

import com.intellij.ide.BrowserUtil;
import com.intellij.openapi.actionSystem.AnAction;
import com.intellij.openapi.actionSystem.AnActionEvent;
import org.jetbrains.annotations.NotNull;

public class OpenDashboardAction extends AnAction {

    private static final String DASHBOARD_URL = "http://127.0.0.1:7777";

    @Override
    public void actionPerformed(@NotNull AnActionEvent e) {
        BrowserUtil.browse(DASHBOARD_URL);
    }
}
