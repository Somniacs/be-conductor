package com.somniacs.beconductor.agent;

import com.intellij.openapi.Disposable;
import com.intellij.openapi.project.Project;
import com.intellij.ui.jcef.JBCefApp;
import com.intellij.ui.jcef.JBCefBrowser;
import com.intellij.util.ui.JBUI;

import javax.swing.*;
import java.awt.*;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;

/**
 * Thin JCEF wrapper that loads the server-served agent-view.html.
 * Used as fallback when HTMLEditorProvider is not available.
 */
public class AgentSessionPanel extends JPanel implements Disposable {

    private JBCefBrowser browser;

    public AgentSessionPanel(Project project, String sessionId) {
        super(new BorderLayout());

        String baseUrl = "http://127.0.0.1:7777";
        String wsBase = "ws://127.0.0.1:7777";
        String url = baseUrl + "/agent/" + URLEncoder.encode(sessionId, StandardCharsets.UTF_8)
                + "?session=" + URLEncoder.encode(sessionId, StandardCharsets.UTF_8)
                + "&ws=" + URLEncoder.encode(wsBase, StandardCharsets.UTF_8);

        if (JBCefApp.isSupported()) {
            browser = new JBCefBrowser(url);
            add(browser.getComponent(), BorderLayout.CENTER);
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

    @Override
    public void dispose() {
        if (browser != null) {
            browser.dispose();
            browser = null;
        }
    }
}
