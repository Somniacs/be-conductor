package com.somniacs.beconductor.agent;

import com.intellij.openapi.application.ApplicationManager;
import com.intellij.openapi.fileEditor.FileEditor;
import com.intellij.openapi.fileEditor.FileEditorManager;
import com.intellij.openapi.fileEditor.FileEditorState;
import com.intellij.openapi.project.Project;
import com.intellij.openapi.util.Disposer;
import com.intellij.openapi.util.UserDataHolderBase;
import com.intellij.openapi.vfs.LocalFileSystem;
import com.intellij.openapi.vfs.VirtualFile;
import com.intellij.ui.jcef.JBCefApp;
import com.intellij.ui.jcef.JBCefBrowser;
import com.intellij.ui.jcef.JBCefClient;
import com.intellij.ui.jcef.JBCefJSQuery;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import org.cef.handler.CefLoadHandler;
import org.cef.handler.CefLoadHandlerAdapter;
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
    private final JBCefClient client;      // owned per-editor — disposed with this editor
    private final JBCefJSQuery jsQuery;    // tracked by client so disposal cascades
    private final CefLoadHandler loadHandler; // kept so we can removeLoadHandler on dispose
    private final JPanel panel;
    private VirtualFile file;

    public AgentFileEditor(Project project, String sessionId, String url) {
        this.name = sessionId + " (Agent)";
        this.panel = new JPanel(new java.awt.BorderLayout());

        if (!JBCefApp.isSupported()) {
            this.browser = null;
            this.client = null;
            this.jsQuery = null;
            this.loadHandler = null;
            panel.add(new JLabel("JCEF not available"), java.awt.BorderLayout.CENTER);
            return;
        }

        // Own a JBCefClient per editor instance instead of borrowing the
        // shared application-wide default. When this editor is disposed,
        // disposing the client releases every handler + JSQuery attached
        // to it — that's what stops the slow memory growth users saw
        // after opening/closing many agent tabs over a session.
        this.client = JBCefApp.getInstance().createClient();
        Disposer.register(this, this.client);

        this.browser = JBCefBrowser.createBuilder()
                .setClient(this.client)
                .setUrl(url)
                .build();
        Disposer.register(this, this.browser);

        panel.add(browser.getComponent(), java.awt.BorderLayout.CENTER);

        // JS bridge for file opening and tab close signals. Registering
        // the JSQuery with Disposer is belt-and-suspenders: the client
        // already owns it, but a direct register makes the intent
        // explicit and survives any refactor that swaps the client.
        // Non-deprecated form — cast to JBCefBrowserBase; the plain
        // JBCefBrowser overload is scheduled for removal upstream.
        this.jsQuery = JBCefJSQuery.create((com.intellij.ui.jcef.JBCefBrowserBase) this.browser);
        Disposer.register(this, this.jsQuery);

        jsQuery.addHandler(request -> {
            try {
                JsonObject json = JsonParser.parseString(request).getAsJsonObject();
                String type = json.has("type") ? json.get("type").getAsString() : "";
                if ("closeTab".equals(type)) {
                    // Agent session ended — close this editor tab
                    ApplicationManager.getApplication().invokeLater(() -> {
                        if (file != null && project != null && !project.isDisposed()) {
                            FileEditorManager.getInstance(project).closeFile(file);
                        }
                    });
                } else if ("openFile".equals(type)) {
                    String path = json.get("path").getAsString();
                    int line = json.has("line") ? json.get("line").getAsInt() : 0;
                    ApplicationManager.getApplication().invokeLater(() -> {
                        VirtualFile vf = LocalFileSystem.getInstance().findFileByPath(path);
                        if (vf != null && project != null && !project.isDisposed()) {
                            FileEditorManager.getInstance(project).openFile(vf, true);
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

        // Inject the cefQuery function after page loads. Keep the handler
        // reference so we can explicitly removeLoadHandler on dispose —
        // clients generally clean up on disposal, but explicit removal
        // guarantees the handler's closure (which captures jsQuery) is
        // unreachable the moment the editor closes.
        this.loadHandler = new CefLoadHandlerAdapter() {
            @Override
            public void onLoadEnd(org.cef.browser.CefBrowser b, org.cef.browser.CefFrame frame, int httpStatusCode) {
                String js = "window.cefQuery = function(obj) { " + jsQuery.inject("obj.request") + " };";
                b.executeJavaScript(js, b.getURL(), 0);
            }
        };
        this.client.addLoadHandler(this.loadHandler, this.browser.getCefBrowser());
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
        // Remove the load handler explicitly before client disposal so its
        // closure — which captured jsQuery — becomes unreachable right
        // away. The registered Disposer children (client, browser,
        // jsQuery) fire via Disposer.dispose(this) automatically; there's
        // no need to call .dispose() on each one here. Do not call
        // browser.dispose() directly — double-dispose on JBCef types can
        // race with the native CEF side.
        if (client != null && loadHandler != null && browser != null) {
            try {
                client.removeLoadHandler(loadHandler, browser.getCefBrowser());
            } catch (Throwable ignored) {}
        }
        Disposer.dispose(this);
    }
}
