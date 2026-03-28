package com.somniacs.beconductor.agent;

import com.intellij.openapi.fileEditor.FileEditor;
import com.intellij.openapi.fileEditor.FileEditorState;
import com.intellij.openapi.util.UserDataHolderBase;
import com.intellij.ui.jcef.JBCefApp;
import com.intellij.ui.jcef.JBCefBrowser;
import org.jetbrains.annotations.Nls;
import org.jetbrains.annotations.NotNull;
import org.jetbrains.annotations.Nullable;

import javax.swing.*;
import java.beans.PropertyChangeListener;

/**
 * A FileEditor that wraps a JCEF browser for agent sessions.
 * Can be opened in the editor area — survives tab moves, splits, and docking.
 */
public class AgentFileEditor extends UserDataHolderBase implements FileEditor {

    private final String name;
    private final JBCefBrowser browser;
    private final JPanel panel;

    public AgentFileEditor(String sessionId, String url) {
        this.name = sessionId + " (Agent)";
        this.browser = JBCefApp.isSupported() ? new JBCefBrowser(url) : null;
        this.panel = new JPanel(new java.awt.BorderLayout());
        if (browser != null) {
            panel.add(browser.getComponent(), java.awt.BorderLayout.CENTER);
        } else {
            panel.add(new JLabel("JCEF not available"), java.awt.BorderLayout.CENTER);
        }
    }

    @Override
    public @NotNull JComponent getComponent() {
        return panel;
    }

    @Override
    public @Nullable JComponent getPreferredFocusedComponent() {
        return browser != null ? browser.getComponent() : panel;
    }

    @Override
    public @Nls(capitalization = Nls.Capitalization.Title) @NotNull String getName() {
        return name;
    }

    @Override
    public void setState(@NotNull FileEditorState state) {}

    @Override
    public boolean isModified() { return false; }

    @Override
    public boolean isValid() { return true; }

    @Override
    public void addPropertyChangeListener(@NotNull PropertyChangeListener listener) {}

    @Override
    public void removePropertyChangeListener(@NotNull PropertyChangeListener listener) {}

    @Override
    public void dispose() {
        if (browser != null) {
            browser.dispose();
        }
    }
}
