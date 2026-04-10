package com.somniacs.beconductor.agent;

import com.intellij.openapi.Disposable;
import com.intellij.openapi.project.Project;
import com.intellij.ui.jcef.JBCefApp;
import com.intellij.ui.jcef.JBCefBrowser;
import com.intellij.ui.jcef.JBCefClient;
import com.intellij.ui.jcef.JBCefJSQuery;
import com.intellij.util.ui.JBUI;
import com.somniacs.beconductor.api.ServerRegistry;
import org.cef.browser.CefBrowser;
import org.cef.browser.CefFrame;
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
                browser = new JBCefBrowser(url);
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
            // Bridge: __beClipNativePaste() — calls CEF's native paste which
            // reads from the OS clipboard and inserts at the focused element.
            JBCefJSQuery pasteQuery = JBCefJSQuery.create(b);
            pasteQuery.addHandler(_ignored -> {
                try {
                    // Use CEF's native paste — same as right-click → Paste
                    b.getCefBrowser().getFocusedFrame().paste();
                } catch (Exception ignored) {}
                return null;
            });

            // __beClipNativeCopy() — CEF's native copy from focused element to OS clipboard
            JBCefJSQuery copyQuery = JBCefJSQuery.create(b);
            copyQuery.addHandler(_ignored -> {
                try {
                    b.getCefBrowser().getFocusedFrame().copy();
                } catch (Exception ignored) {}
                return null;
            });

            // __beClipNativeCut() — CEF's native cut
            JBCefJSQuery cutQuery = JBCefJSQuery.create(b);
            cutQuery.addHandler(_ignored -> {
                try {
                    b.getCefBrowser().getFocusedFrame().cut();
                } catch (Exception ignored) {}
                return null;
            });

            // Also expose AWT-based read/write as fallback for messages-area copy
            JBCefJSQuery awtWriteQuery = JBCefJSQuery.create(b);
            awtWriteQuery.addHandler(text -> {
                try {
                    Clipboard sys = Toolkit.getDefaultToolkit().getSystemClipboard();
                    sys.setContents(new StringSelection(text != null ? text : ""), null);
                } catch (Exception ignored) {}
                return null;
            });

            // Inject bridge functions into the page after it loads
            b.getJBCefClient().addLoadHandler(new CefLoadHandlerAdapter() {
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
            }, b.getCefBrowser());
        } catch (Throwable t) {
            // Older IDE builds may not expose these APIs — fall back to
            // context menu paste. Don't crash the plugin.
        }
    }

    @Override
    public void dispose() {
        if (browser != null) {
            browser.dispose();
            browser = null;
        }
    }
}
