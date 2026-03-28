package com.somniacs.beconductor.agent;

import com.intellij.openapi.application.ApplicationManager;
import com.intellij.openapi.fileEditor.FileEditor;
import com.intellij.openapi.fileEditor.FileEditorManager;
import com.intellij.openapi.fileEditor.FileEditorState;
import com.intellij.openapi.project.Project;
import com.intellij.openapi.util.UserDataHolderBase;
import com.intellij.openapi.vfs.LocalFileSystem;
import com.intellij.openapi.vfs.VirtualFile;
import com.intellij.ui.jcef.JBCefApp;
import com.intellij.ui.jcef.JBCefBrowser;
import com.intellij.ui.jcef.JBCefJSQuery;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import org.jetbrains.annotations.Nls;
import org.jetbrains.annotations.NotNull;
import org.jetbrains.annotations.Nullable;

import javax.swing.*;
import java.beans.PropertyChangeListener;

/**
 * A FileEditor that wraps a JCEF browser for agent sessions.
 * Can be opened in the editor area — survives tab moves, splits, and docking.
 * Registers a JBCefJSQuery bridge so JavaScript can open files in the IDE.
 */
public class AgentFileEditor extends UserDataHolderBase implements FileEditor {

    private final String name;
    private final JBCefBrowser browser;
    private final JPanel panel;
    private VirtualFile file;
    private JBCefJSQuery jsQuery;

    public AgentFileEditor(Project project, String sessionId, String url) {
        this.name = sessionId + " (Agent)";
        this.browser = JBCefApp.isSupported() ? new JBCefBrowser(url) : null;
        this.panel = new JPanel(new java.awt.BorderLayout());
        if (browser != null) {
            panel.add(browser.getComponent(), java.awt.BorderLayout.CENTER);

            // Register JS bridge for file opening
            jsQuery = JBCefJSQuery.create(browser);
            jsQuery.addHandler(request -> {
                try {
                    JsonObject json = JsonParser.parseString(request).getAsJsonObject();
                    String type = json.has("type") ? json.get("type").getAsString() : "";
                    if ("openFile".equals(type)) {
                        String path = json.get("path").getAsString();
                        int line = json.has("line") ? json.get("line").getAsInt() : 0;
                        ApplicationManager.getApplication().invokeLater(() -> {
                            VirtualFile vf = LocalFileSystem.getInstance().findFileByPath(path);
                            if (vf != null && project != null && !project.isDisposed()) {
                                FileEditorManager.getInstance(project).openFile(vf, true);
                                // Navigate to line
                                if (line > 0) {
                                    var editors = FileEditorManager.getInstance(project).getEditors(vf);
                                    for (var ed : editors) {
                                        if (ed instanceof com.intellij.openapi.fileEditor.TextEditor te) {
                                            var editor = te.getEditor();
                                            int offset = editor.getDocument().getLineStartOffset(Math.min(line - 1, editor.getDocument().getLineCount() - 1));
                                            editor.getCaretModel().moveToOffset(offset);
                                            editor.getScrollingModel().scrollToCaret(com.intellij.openapi.editor.ScrollType.CENTER);
                                        }
                                    }
                                }
                            }
                        });
                    }
                } catch (Exception e) { /* ignore parse errors */ }
                return new JBCefJSQuery.Response("");
            });

            // Inject the cefQuery function after page loads
            browser.getJBCefClient().addLoadHandler(new org.cef.handler.CefLoadHandlerAdapter() {
                @Override
                public void onLoadEnd(org.cef.browser.CefBrowser b, org.cef.browser.CefFrame frame, int httpStatusCode) {
                    String js = "window.cefQuery = function(obj) { " + jsQuery.inject("obj.request") + " };";
                    b.executeJavaScript(js, b.getURL(), 0);
                }
            }, browser.getCefBrowser());
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

    public void setFile(@NotNull VirtualFile file) {
        this.file = file;
    }

    @Override
    public @NotNull VirtualFile getFile() {
        return file;
    }

    @Override
    public void dispose() {
        if (browser != null) {
            browser.dispose();
        }
    }
}
