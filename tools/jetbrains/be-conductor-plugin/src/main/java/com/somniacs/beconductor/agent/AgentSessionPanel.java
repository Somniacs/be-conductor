package com.somniacs.beconductor.agent;

import com.intellij.openapi.Disposable;
import com.intellij.openapi.project.Project;
import com.intellij.openapi.util.Disposer;
import com.intellij.ui.jcef.JBCefApp;
import com.intellij.ui.jcef.JBCefBrowser;
import com.intellij.ui.jcef.JBCefBrowserBase;
import com.intellij.ui.jcef.JBCefClient;
import com.intellij.ui.jcef.JBCefJSQuery;
import com.intellij.util.ui.JBUI;
import com.somniacs.beconductor.api.ServerRegistry;
import org.cef.browser.CefBrowser;
import org.cef.browser.CefFrame;
import org.cef.handler.CefLoadHandler;
import org.cef.handler.CefLoadHandlerAdapter;

import javax.swing.*;
import java.awt.*;
import java.awt.datatransfer.Clipboard;
import java.awt.datatransfer.DataFlavor;
import java.awt.datatransfer.StringSelection;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;

/**
 * Thin JCEF wrapper that loads the server-served agent-view.html.
 * Used as fallback when HTMLEditorProvider is not available.
 */
public class AgentSessionPanel extends JPanel implements Disposable {

    private JBCefBrowser browser;
    private JBCefClient client;                 // owned per-panel
    private CefLoadHandler bridgeLoadHandler;   // kept so we can removeLoadHandler

    public AgentSessionPanel(Project project, String serverKey, String sessionId) {
        super(new BorderLayout());

        ServerRegistry registry = ServerRegistry.getInstance();
        String baseUrl = registry.getBaseUrl(serverKey);
        String wsBase = baseUrl.replaceFirst("^http", "ws");
        // Append a timestamp to bust JCEF's disk cache on every tab open —
        // otherwise the embedded browser may serve a stale agent-view.html
        // even after server updates.
        String url = baseUrl + "/agent/" + URLEncoder.encode(sessionId, StandardCharsets.UTF_8)
                + "?session=" + URLEncoder.encode(sessionId, StandardCharsets.UTF_8)
                + "&ws=" + URLEncoder.encode(wsBase, StandardCharsets.UTF_8)
                + "&_v=" + System.currentTimeMillis();

        if (JBCefApp.isSupported()) {
            // Defer browser creation so the panel is already in a visible window
            // hierarchy — avoids black screen when used inside tool windows.
            SwingUtilities.invokeLater(() -> {
                if (browser != null) return; // guard against double-init
                // Own the JBCefClient per-panel so every handler + JSQuery
                // attached to it is released when the panel is disposed.
                // Previously we borrowed the shared application-wide
                // default client, which retained 4 JSQueries + 1 load
                // handler per open/close cycle. That was the source of
                // the Rider memory growth over a long day of use.
                client = JBCefApp.getInstance().createClient();
                Disposer.register(this, client);
                browser = JBCefBrowser.createBuilder()
                        .setClient(client)
                        .setUrl(url)
                        .build();
                Disposer.register(this, browser);
                installClipboardBridge(browser);
                add(browser.getComponent(), BorderLayout.CENTER);
                revalidate();
                repaint();
            });
        } else {
            // Fallback: show a message with a link
            JLabel label = new JLabel(
                    "<html><body style='padding:20px;'>"
                    + "<p>JCEF (embedded browser) is not available in this IDE.</p>"
                    + "<p>Open the agent session in your browser instead.</p>"
                    + "</body></html>");
            label.setBorder(JBUI.Borders.empty(20));
            add(label, BorderLayout.CENTER);

            JButton openBtn = new JButton("Open in Browser");
            openBtn.addActionListener(e -> com.intellij.ide.BrowserUtil.browse(url));
            JPanel btnPanel = new JPanel(new FlowLayout(FlowLayout.LEFT));
            btnPanel.add(openBtn);
            add(btnPanel, BorderLayout.SOUTH);
        }
    }

    /** Convenience constructor for local server. */
    public AgentSessionPanel(Project project, String sessionId) {
        this(project, "local", sessionId);
    }

    /**
     * Bridge the JCEF webview's clipboard to the OS system clipboard.
     *
     * JCEF doesn't sync the JS Clipboard API with the OS system clipboard,
     * but CEF's native CefBrowser.copy()/paste()/cut() methods DO use the
     * actual system clipboard (that's what the right-click context menu
     * uses). We expose these as JS functions and override the textarea's
     * Ctrl+C/V/X to call them.
     */
    private void installClipboardBridge(JBCefBrowser b) {
        try {
            // All four JSQueries and the load handler are attached to the
            // per-panel client (owned in the outer scope). Registering
            // each JSQuery with Disposer tracks intent explicitly — the
            // client's disposal also clears them, but an explicit
            // register makes this audit-safe the next time someone
            // touches the code.
            JBCefJSQuery pasteQuery = JBCefJSQuery.create((JBCefBrowserBase) b);
            Disposer.register(this, pasteQuery);
            pasteQuery.addHandler(_ignored -> {
                try {
                    b.getCefBrowser().getFocusedFrame().paste();
                } catch (Exception ignored) {}
                return null;
            });

            JBCefJSQuery copyQuery = JBCefJSQuery.create((JBCefBrowserBase) b);
            Disposer.register(this, copyQuery);
            copyQuery.addHandler(_ignored -> {
                try {
                    b.getCefBrowser().getFocusedFrame().copy();
                } catch (Exception ignored) {}
                return null;
            });

            JBCefJSQuery cutQuery = JBCefJSQuery.create((JBCefBrowserBase) b);
            Disposer.register(this, cutQuery);
            cutQuery.addHandler(_ignored -> {
                try {
                    b.getCefBrowser().getFocusedFrame().cut();
                } catch (Exception ignored) {}
                return null;
            });

            JBCefJSQuery awtWriteQuery = JBCefJSQuery.create((JBCefBrowserBase) b);
            Disposer.register(this, awtWriteQuery);
            awtWriteQuery.addHandler(text -> {
                try {
                    Clipboard sys = Toolkit.getDefaultToolkit().getSystemClipboard();
                    sys.setContents(new StringSelection(text != null ? text : ""), null);
                } catch (Exception ignored) {}
                return null;
            });

            // Inject bridge functions via a handler attached to the owned
            // client. The handler reference is kept on the panel so
            // dispose() can removeLoadHandler before the client itself
            // is disposed — making the handler's closure (which
            // retains all four JSQueries above) unreachable promptly.
            bridgeLoadHandler = new CefLoadHandlerAdapter() {
                @Override
                public void onLoadEnd(CefBrowser cefBrowser, CefFrame frame, int httpStatusCode) {
                    if (!frame.isMain()) return;
                    String js = ""
                        + "window.__beClipNativePaste = function() {" + pasteQuery.inject("") + "};"
                        + "window.__beClipNativeCopy = function() {" + copyQuery.inject("") + "};"
                        + "window.__beClipNativeCut = function() {" + cutQuery.inject("") + "};"
                        + "window.__beClipWrite = function(text) {" + awtWriteQuery.inject("text") + "};";
                    cefBrowser.executeJavaScript(js, cefBrowser.getURL(), 0);
                }
            };
            client.addLoadHandler(bridgeLoadHandler, b.getCefBrowser());
        } catch (Throwable t) {
            // Older IDE builds may not expose these APIs — fall back to
            // context menu paste. Don't crash the plugin.
        }
    }

    @Override
    public void dispose() {
        // Remove our load handler from the client before Disposer fires —
        // drops the only strong reference to the handler's closure (which
        // retained four JSQueries). The client, browser, and JSQueries
        // were all registered as Disposer children, so Disposer.dispose
        // walks them in reverse-registration order.
        if (client != null && bridgeLoadHandler != null && browser != null) {
            try {
                client.removeLoadHandler(bridgeLoadHandler, browser.getCefBrowser());
            } catch (Throwable ignored) {}
        }
        bridgeLoadHandler = null;
        Disposer.dispose(this);
        browser = null;
        client = null;
    }
}
