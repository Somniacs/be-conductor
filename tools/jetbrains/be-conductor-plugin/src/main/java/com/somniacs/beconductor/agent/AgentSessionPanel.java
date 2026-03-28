package com.somniacs.beconductor.agent;

import com.intellij.openapi.Disposable;
import com.intellij.openapi.project.Project;
import com.intellij.ui.JBColor;
import com.intellij.ui.components.JBLabel;
import com.intellij.ui.components.JBScrollPane;
import com.intellij.ui.components.JBTextArea;
import com.intellij.util.ui.JBUI;
import com.google.gson.Gson;

import javax.swing.*;
import javax.swing.text.html.HTMLEditorKit;
import javax.swing.text.html.StyleSheet;
import java.awt.*;
import java.awt.event.*;
import java.util.List;
import java.util.Map;

/**
 * Native Swing panel that renders agent session events.
 * Replaces the JCEF browser-based approach with a lightweight,
 * theme-aware rendering using IntelliJ Platform SDK components.
 */
public class AgentSessionPanel extends JPanel implements Disposable,
        AgentWebSocketClient.AgentEventListener {

    // ── Colors (theme-aware) ────────────────────────────────────────────

    private static final JBColor USER_BORDER_COLOR = new JBColor(
            new Color(0x40, 0x80, 0xf0), new Color(0x4a, 0x6c, 0xf7));
    private static final JBColor ASSISTANT_BG = new JBColor(
            new Color(0xf0, 0xf4, 0xf8), new Color(0x2b, 0x2d, 0x30));
    private static final JBColor TOOL_RESULT_OK_COLOR = new JBColor(
            new Color(0x28, 0xa7, 0x45), new Color(0x44, 0xbb, 0x77));
    private static final JBColor TOOL_RESULT_ERR_COLOR = new JBColor(
            new Color(0xd0, 0x30, 0x30), new Color(0xe0, 0x50, 0x50));
    private static final JBColor RESULT_BG = new JBColor(
            new Color(0xe8, 0xf5, 0xe9), new Color(0x1b, 0x3a, 0x1b));
    private static final JBColor ERROR_BG = new JBColor(
            new Color(0xff, 0xeb, 0xee), new Color(0x3a, 0x1b, 0x1b));
    private static final JBColor THINKING_HEADER_FG = new JBColor(
            new Color(0x88, 0x88, 0x88), new Color(0x88, 0x88, 0x88));
    private static final JBColor TOOL_HEADER_FG = new JBColor(
            new Color(0xc0, 0x8a, 0x20), new Color(0xe0, 0xb0, 0x40));
    private static final JBColor MUTED_FG = new JBColor(
            new Color(0x99, 0x99, 0x99), new Color(0x77, 0x77, 0x77));
    private static final JBColor CODE_BG = new JBColor(
            new Color(0xf5, 0xf5, 0xf5), new Color(0x2d, 0x2d, 0x2d));

    // ── Components ──────────────────────────────────────────────────────

    private final Project project;
    private final String sessionId;
    private final AgentWebSocketClient wsClient;

    private final JPanel messagesPanel;
    private final JBScrollPane scrollPane;
    private final JBTextArea inputArea;
    private final JButton sendStopBtn;
    private final JButton modeBtn;
    private final JButton effortLBtn;
    private final JButton effortMBtn;
    private final JButton effortHBtn;
    private final JButton attachBtn;
    private final JBLabel statusLabel;

    private volatile boolean busy = false;
    private String currentMode = "default";
    private String currentEffort = "high";

    public AgentSessionPanel(Project project, String sessionId) {
        super(new BorderLayout());
        this.project = project;
        this.sessionId = sessionId;

        // ── Messages area (CENTER) ──────────────────────────────────────
        messagesPanel = new JPanel();
        messagesPanel.setLayout(new BoxLayout(messagesPanel, BoxLayout.Y_AXIS));
        messagesPanel.setBorder(JBUI.Borders.empty(8));

        scrollPane = new JBScrollPane(messagesPanel,
                ScrollPaneConstants.VERTICAL_SCROLLBAR_AS_NEEDED,
                ScrollPaneConstants.HORIZONTAL_SCROLLBAR_NEVER);
        add(scrollPane, BorderLayout.CENTER);

        // ── Status bar ──────────────────────────────────────────────────
        statusLabel = new JBLabel("Connecting...");
        statusLabel.setBorder(JBUI.Borders.empty(2, 8));
        statusLabel.setForeground(MUTED_FG);

        // ── Input area (SOUTH) ──────────────────────────────────────────
        JPanel inputPanel = new JPanel(new BorderLayout(4, 4));
        inputPanel.setBorder(JBUI.Borders.empty(4, 8, 8, 8));

        inputArea = new JBTextArea(2, 40);
        inputArea.setLineWrap(true);
        inputArea.setWrapStyleWord(true);
        inputArea.setFont(JBUI.Fonts.label());
        inputArea.setBorder(BorderFactory.createCompoundBorder(
                BorderFactory.createLineBorder(JBColor.border()),
                JBUI.Borders.empty(4, 6)));
        inputArea.addKeyListener(new KeyAdapter() {
            @Override
            public void keyPressed(KeyEvent e) {
                if (e.getKeyCode() == KeyEvent.VK_ENTER && !e.isShiftDown()) {
                    e.consume();
                    onSendOrStop();
                }
            }
        });

        // Auto-grow input area (up to 8 lines)
        inputArea.getDocument().addDocumentListener(new javax.swing.event.DocumentListener() {
            public void insertUpdate(javax.swing.event.DocumentEvent e) { adjustHeight(); }
            public void removeUpdate(javax.swing.event.DocumentEvent e) { adjustHeight(); }
            public void changedUpdate(javax.swing.event.DocumentEvent e) { adjustHeight(); }
            private void adjustHeight() {
                int lineCount = inputArea.getLineCount();
                int rows = Math.max(2, Math.min(lineCount, 8));
                inputArea.setRows(rows);
                inputPanel.revalidate();
            }
        });

        JBScrollPane inputScroll = new JBScrollPane(inputArea,
                ScrollPaneConstants.VERTICAL_SCROLLBAR_AS_NEEDED,
                ScrollPaneConstants.HORIZONTAL_SCROLLBAR_NEVER);
        inputPanel.add(inputScroll, BorderLayout.CENTER);

        // Button panel (right side)
        JPanel buttonPanel = new JPanel();
        buttonPanel.setLayout(new BoxLayout(buttonPanel, BoxLayout.Y_AXIS));

        sendStopBtn = new JButton("Send");
        sendStopBtn.setToolTipText("Send prompt (Enter)");
        sendStopBtn.addActionListener(e -> onSendOrStop());
        sendStopBtn.setAlignmentX(Component.CENTER_ALIGNMENT);
        buttonPanel.add(sendStopBtn);
        buttonPanel.add(Box.createVerticalStrut(4));

        attachBtn = new JButton("+");
        attachBtn.setToolTipText("Attach file");
        attachBtn.setMargin(JBUI.insets(2, 6));
        attachBtn.addActionListener(e -> onAttachFile());
        attachBtn.setAlignmentX(Component.CENTER_ALIGNMENT);
        buttonPanel.add(attachBtn);

        inputPanel.add(buttonPanel, BorderLayout.EAST);

        // Controls bar (below input: mode, effort, status)
        JPanel controlsBar = new JPanel(new FlowLayout(FlowLayout.LEFT, 4, 2));

        modeBtn = new JButton("Mode: Default");
        modeBtn.setToolTipText("Change agent permission mode");
        modeBtn.setFont(JBUI.Fonts.smallFont());
        modeBtn.setMargin(JBUI.insets(1, 6));
        modeBtn.addActionListener(e -> showModePopup());
        controlsBar.add(modeBtn);

        controlsBar.add(Box.createHorizontalStrut(8));
        controlsBar.add(new JBLabel("Effort:"));

        effortLBtn = createEffortButton("L", "low");
        effortMBtn = createEffortButton("M", "medium");
        effortHBtn = createEffortButton("H", "high");
        controlsBar.add(effortLBtn);
        controlsBar.add(effortMBtn);
        controlsBar.add(effortHBtn);
        updateEffortButtons();

        controlsBar.add(Box.createHorizontalGlue());
        controlsBar.add(statusLabel);

        JPanel southPanel = new JPanel(new BorderLayout());
        southPanel.add(controlsBar, BorderLayout.NORTH);
        southPanel.add(inputPanel, BorderLayout.CENTER);
        add(southPanel, BorderLayout.SOUTH);

        // ── WebSocket connection ────────────────────────────────────────
        wsClient = new AgentWebSocketClient(this);
        wsClient.connect(this.sessionId);
    }

    // ── Disposable ──────────────────────────────────────────────────────

    @Override
    public void dispose() {
        wsClient.close();
    }

    // ── AgentEventListener ──────────────────────────────────────────────

    @Override
    public void onEvent(Map<String, Object> event) {
        // Already on EDT (dispatched by AgentWebSocketClient)
        String type = event.get("type") instanceof String t ? t : "";
        switch (type) {
            case "user_message" -> renderUserMessage(event);
            case "assistant_message" -> renderAssistantMessage(event);
            case "result" -> renderResult(event);
            case "error" -> renderError(event);
            case "session_end" -> renderSessionEnd(event);
            case "system" -> renderSystem(event);
            case "rate_limit" -> renderRateLimit(event);
            default -> { /* ignore ping, unknown types */ }
        }
    }

    @Override
    public void onConnected() {
        statusLabel.setText("Connected");
        statusLabel.setForeground(TOOL_RESULT_OK_COLOR);
        inputArea.setEnabled(true);
        sendStopBtn.setEnabled(true);
    }

    @Override
    public void onDisconnected() {
        statusLabel.setText("Disconnected");
        statusLabel.setForeground(TOOL_RESULT_ERR_COLOR);
    }

    // ── Input handling ──────────────────────────────────────────────────

    private void onSendOrStop() {
        if (busy) {
            wsClient.sendInterrupt();
            return;
        }
        String text = inputArea.getText().trim();
        if (text.isEmpty()) return;
        inputArea.setText("");
        wsClient.sendPrompt(text);
    }

    private void onAttachFile() {
        JFileChooser chooser = new JFileChooser();
        chooser.setMultiSelectionEnabled(true);
        if (project.getBasePath() != null) {
            chooser.setCurrentDirectory(new java.io.File(project.getBasePath()));
        }
        int result = chooser.showOpenDialog(this);
        if (result == JFileChooser.APPROVE_OPTION) {
            java.io.File[] files = chooser.getSelectedFiles();
            for (java.io.File file : files) {
                String mention = "[file:" + file.getAbsolutePath() + "]";
                inputArea.append((inputArea.getText().isEmpty() ? "" : " ") + mention);
            }
            inputArea.requestFocusInWindow();
        }
    }

    private void showModePopup() {
        JPopupMenu popup = new JPopupMenu();

        addModeItem(popup, "Default", "default",
                "Normal mode - asks for permission on file changes");
        addModeItem(popup, "Plan", "plan",
                "Planning only - no code changes, just analysis");
        addModeItem(popup, "Auto (Accept Edits)", "acceptEdits",
                "Auto-approve all file edits");

        popup.show(modeBtn, 0, modeBtn.getHeight());
    }

    private void addModeItem(JPopupMenu popup, String label, String mode, String description) {
        JMenuItem item = new JMenuItem(label + " - " + description);
        if (mode.equals(currentMode)) {
            item.setFont(item.getFont().deriveFont(Font.BOLD));
        }
        item.addActionListener(e -> {
            currentMode = mode;
            modeBtn.setText("Mode: " + label);
            wsClient.setMode(mode);
        });
        popup.add(item);
    }

    private JButton createEffortButton(String label, String effort) {
        JButton btn = new JButton(label);
        btn.setFont(JBUI.Fonts.smallFont());
        btn.setMargin(JBUI.insets(1, 4));
        btn.setToolTipText("Set effort: " + effort);
        btn.addActionListener(e -> {
            currentEffort = effort;
            updateEffortButtons();
            wsClient.setEffort(effort);
        });
        return btn;
    }

    private void updateEffortButtons() {
        effortLBtn.setEnabled(!"low".equals(currentEffort));
        effortMBtn.setEnabled(!"medium".equals(currentEffort));
        effortHBtn.setEnabled(!"high".equals(currentEffort));
    }

    private void setBusy(boolean b) {
        busy = b;
        sendStopBtn.setText(b ? "Stop" : "Send");
        sendStopBtn.setToolTipText(b ? "Interrupt agent" : "Send prompt (Enter)");
    }

    // ── Event rendering ─────────────────────────────────────────────────

    private void renderUserMessage(Map<String, Object> event) {
        setBusy(true);
        String content = event.get("content") instanceof String s ? s : "";

        JPanel panel = new JPanel(new BorderLayout());
        panel.setBorder(BorderFactory.createCompoundBorder(
                BorderFactory.createMatteBorder(0, 3, 0, 0, USER_BORDER_COLOR),
                JBUI.Borders.empty(8, 12, 8, 8)));
        panel.setMaximumSize(new Dimension(Integer.MAX_VALUE, panel.getPreferredSize().height));

        JBLabel label = new JBLabel("<html><b>You</b></html>");
        label.setForeground(USER_BORDER_COLOR);
        panel.add(label, BorderLayout.NORTH);

        JTextArea textArea = createReadOnlyTextArea(content);
        panel.add(textArea, BorderLayout.CENTER);

        addMessagePanel(panel);
    }

    @SuppressWarnings("unchecked")
    private void renderAssistantMessage(Map<String, Object> event) {
        setBusy(true);
        Object contentObj = event.get("content");
        if (!(contentObj instanceof List<?> blocks)) return;

        JPanel panel = new JPanel();
        panel.setLayout(new BoxLayout(panel, BoxLayout.Y_AXIS));
        panel.setBorder(JBUI.Borders.empty(8, 8, 8, 8));
        panel.setBackground(ASSISTANT_BG);
        panel.setOpaque(true);

        for (Object blockObj : blocks) {
            if (!(blockObj instanceof Map<?, ?> blockMap)) continue;
            Map<String, Object> block = (Map<String, Object>) blockMap;
            String btype = block.get("type") instanceof String t ? t : "";

            switch (btype) {
                case "text" -> {
                    String text = block.get("text") instanceof String s ? s : "";
                    JEditorPane htmlPane = createHtmlPane(markdownToHtml(text));
                    panel.add(htmlPane);
                }
                case "thinking" -> {
                    String thinking = block.get("thinking") instanceof String s ? s : "";
                    panel.add(createCollapsiblePanel(
                            "Thinking...", THINKING_HEADER_FG, thinking, false));
                }
                case "tool_use" -> {
                    String tool = block.get("tool") instanceof String s ? s : "tool";
                    Object input = block.get("input");
                    String inputStr = input != null ? new Gson().toJson(input) : "{}";
                    String summary = summarizeToolInput(tool, input);
                    panel.add(createCollapsiblePanel(
                            tool + " - " + summary, TOOL_HEADER_FG, inputStr, false));
                }
                case "tool_result" -> {
                    String content = block.get("content") instanceof String s ? s : "";
                    boolean isError = Boolean.TRUE.equals(block.get("is_error"));
                    JBColor borderColor = isError ? TOOL_RESULT_ERR_COLOR : TOOL_RESULT_OK_COLOR;

                    JPanel resultPanel = new JPanel(new BorderLayout());
                    resultPanel.setBorder(BorderFactory.createCompoundBorder(
                            BorderFactory.createMatteBorder(0, 3, 0, 0, borderColor),
                            JBUI.Borders.empty(4, 8, 4, 4)));

                    String truncated = content.length() > 2000
                            ? content.substring(0, 2000) + "\n... (truncated)"
                            : content;
                    JTextArea resultText = createMonospaceTextArea(truncated);
                    resultPanel.add(resultText, BorderLayout.CENTER);
                    resultPanel.setAlignmentX(Component.LEFT_ALIGNMENT);
                    resultPanel.setMaximumSize(new Dimension(
                            Integer.MAX_VALUE, resultPanel.getPreferredSize().height));
                    panel.add(resultPanel);
                }
            }
            panel.add(Box.createVerticalStrut(4));
        }

        addMessagePanel(panel);
    }

    private void renderResult(Map<String, Object> event) {
        setBusy(false);
        String result = event.get("result") instanceof String s ? s : "Done";
        Object costObj = event.get("total_cost_usd");
        Object turnsObj = event.get("num_turns");
        Object durationObj = event.get("duration_ms");

        StringBuilder meta = new StringBuilder();
        if (turnsObj instanceof Number n && n.intValue() > 0) {
            meta.append(n.intValue()).append(" turn(s)");
        }
        if (durationObj instanceof Number n && n.longValue() > 0) {
            long ms = n.longValue();
            if (meta.length() > 0) meta.append("  |  ");
            if (ms >= 60000) {
                meta.append(String.format("%.1f min", ms / 60000.0));
            } else {
                meta.append(String.format("%.1fs", ms / 1000.0));
            }
        }
        if (costObj instanceof Number n && n.doubleValue() > 0) {
            if (meta.length() > 0) meta.append("  |  ");
            meta.append(String.format("$%.4f", n.doubleValue()));
        }

        JPanel panel = new JPanel(new BorderLayout());
        panel.setBackground(RESULT_BG);
        panel.setOpaque(true);
        panel.setBorder(JBUI.Borders.empty(8, 12));

        JBLabel doneLabel = new JBLabel("Done");
        doneLabel.setFont(doneLabel.getFont().deriveFont(Font.BOLD));
        doneLabel.setForeground(TOOL_RESULT_OK_COLOR);
        panel.add(doneLabel, BorderLayout.NORTH);

        if (result != null && !result.isEmpty()) {
            JTextArea resultText = createReadOnlyTextArea(result);
            panel.add(resultText, BorderLayout.CENTER);
        }

        if (meta.length() > 0) {
            JBLabel metaLabel = new JBLabel(meta.toString());
            metaLabel.setForeground(MUTED_FG);
            metaLabel.setFont(JBUI.Fonts.smallFont());
            panel.add(metaLabel, BorderLayout.SOUTH);
        }

        addMessagePanel(panel);
    }

    private void renderError(Map<String, Object> event) {
        setBusy(false);
        String error = event.get("error") instanceof String s ? s : "Unknown error";

        JPanel panel = new JPanel(new BorderLayout());
        panel.setBackground(ERROR_BG);
        panel.setOpaque(true);
        panel.setBorder(JBUI.Borders.empty(8, 12));

        JBLabel errorLabel = new JBLabel("Error");
        errorLabel.setFont(errorLabel.getFont().deriveFont(Font.BOLD));
        errorLabel.setForeground(TOOL_RESULT_ERR_COLOR);
        panel.add(errorLabel, BorderLayout.NORTH);

        JTextArea errorText = createReadOnlyTextArea(error);
        errorText.setForeground(TOOL_RESULT_ERR_COLOR);
        panel.add(errorText, BorderLayout.CENTER);

        addMessagePanel(panel);
    }

    private void renderSessionEnd(Map<String, Object> event) {
        setBusy(false);
        Object exitCodeObj = event.get("exit_code");
        String code = exitCodeObj instanceof Number n ? String.valueOf(n.intValue()) : "0";

        JPanel panel = new JPanel(new FlowLayout(FlowLayout.CENTER));
        JBLabel label = new JBLabel("Session ended (exit " + code + ")");
        label.setForeground(MUTED_FG);
        label.setFont(JBUI.Fonts.smallFont());
        panel.add(label);

        addMessagePanel(panel);
    }

    private void renderSystem(Map<String, Object> event) {
        String subtype = event.get("subtype") instanceof String s ? s : "";
        if ("init".equals(subtype)) {
            JPanel panel = new JPanel(new FlowLayout(FlowLayout.CENTER));
            JBLabel label = new JBLabel("Agent initialized");
            label.setForeground(MUTED_FG);
            label.setFont(JBUI.Fonts.smallFont());
            panel.add(label);
            addMessagePanel(panel);
        }
    }

    private void renderRateLimit(Map<String, Object> event) {
        String info = event.get("info") instanceof String s ? s : "Rate limited";

        JPanel panel = new JPanel(new FlowLayout(FlowLayout.CENTER));
        JBLabel label = new JBLabel("Rate limited: " + info);
        label.setForeground(TOOL_HEADER_FG);
        label.setFont(JBUI.Fonts.smallFont());
        panel.add(label);
        addMessagePanel(panel);
    }

    // ── UI helpers ──────────────────────────────────────────────────────

    private void addMessagePanel(JPanel panel) {
        panel.setAlignmentX(Component.LEFT_ALIGNMENT);
        // Constrain height to preferred, allow full width
        Dimension pref = panel.getPreferredSize();
        panel.setMaximumSize(new Dimension(Integer.MAX_VALUE, pref.height));

        messagesPanel.add(panel);
        messagesPanel.add(Box.createVerticalStrut(6));
        messagesPanel.revalidate();
        messagesPanel.repaint();

        // Auto-scroll if near bottom
        SwingUtilities.invokeLater(() -> {
            JScrollBar vsb = scrollPane.getVerticalScrollBar();
            int extent = vsb.getModel().getExtent();
            int max = vsb.getModel().getMaximum();
            int value = vsb.getValue();
            // "Near bottom" = within 150px of the end
            if (max - (value + extent) < 150) {
                vsb.setValue(max);
            }
        });
    }

    private static JTextArea createReadOnlyTextArea(String text) {
        JTextArea area = new JTextArea(text);
        area.setEditable(false);
        area.setLineWrap(true);
        area.setWrapStyleWord(true);
        area.setOpaque(false);
        area.setFont(JBUI.Fonts.label());
        area.setBorder(JBUI.Borders.empty(4, 0));
        return area;
    }

    private static JTextArea createMonospaceTextArea(String text) {
        JTextArea area = new JTextArea(text);
        area.setEditable(false);
        area.setLineWrap(true);
        area.setWrapStyleWord(false);
        area.setOpaque(false);
        area.setFont(JBUI.Fonts.create(Font.MONOSPACED, 12));
        area.setBorder(JBUI.Borders.empty(2, 0));
        return area;
    }

    private JEditorPane createHtmlPane(String html) {
        JEditorPane pane = new JEditorPane();
        pane.setContentType("text/html");
        pane.setEditable(false);
        pane.setOpaque(false);

        HTMLEditorKit kit = new HTMLEditorKit();
        StyleSheet styleSheet = kit.getStyleSheet();
        Color fg = UIManager.getColor("Label.foreground");
        String fgHex = fg != null ? String.format("#%02x%02x%02x", fg.getRed(), fg.getGreen(), fg.getBlue()) : "#cccccc";
        Color codeBg = CODE_BG;
        String codeBgHex = String.format("#%02x%02x%02x", codeBg.getRed(), codeBg.getGreen(), codeBg.getBlue());
        styleSheet.addRule("body { font-family: sans-serif; font-size: 13px; color: " + fgHex + "; margin: 4px 0; }");
        styleSheet.addRule("code { font-family: monospace; background: " + codeBgHex + "; padding: 1px 4px; border-radius: 3px; }");
        styleSheet.addRule("pre { font-family: monospace; background: " + codeBgHex + "; padding: 8px; margin: 4px 0; white-space: pre-wrap; word-wrap: break-word; }");
        styleSheet.addRule("li { margin-left: 16px; }");
        pane.setEditorKit(kit);

        pane.setText("<html><body>" + html + "</body></html>");
        pane.setAlignmentX(Component.LEFT_ALIGNMENT);
        pane.setMaximumSize(new Dimension(Integer.MAX_VALUE, pane.getPreferredSize().height));
        return pane;
    }

    /**
     * Create a collapsible panel with a clickable header and expandable content.
     */
    private JPanel createCollapsiblePanel(String title, Color titleColor,
                                          String content, boolean expandedByDefault) {
        JPanel wrapper = new JPanel(new BorderLayout());
        wrapper.setOpaque(false);
        wrapper.setAlignmentX(Component.LEFT_ALIGNMENT);

        JPanel header = new JPanel(new FlowLayout(FlowLayout.LEFT, 4, 2));
        header.setOpaque(false);
        header.setCursor(Cursor.getPredefinedCursor(Cursor.HAND_CURSOR));

        JBLabel arrow = new JBLabel(expandedByDefault ? "\u25BC" : "\u25B6");
        arrow.setForeground(MUTED_FG);
        header.add(arrow);

        JBLabel titleLabel = new JBLabel(title);
        titleLabel.setForeground(titleColor);
        titleLabel.setFont(titleLabel.getFont().deriveFont(Font.BOLD, 12f));
        header.add(titleLabel);

        JTextArea contentArea = createMonospaceTextArea(content);
        contentArea.setVisible(expandedByDefault);
        contentArea.setBorder(JBUI.Borders.empty(4, 24, 4, 4));

        header.addMouseListener(new MouseAdapter() {
            @Override
            public void mouseClicked(MouseEvent e) {
                boolean visible = !contentArea.isVisible();
                contentArea.setVisible(visible);
                arrow.setText(visible ? "\u25BC" : "\u25B6");
                // Recompute sizes up the chain
                revalidateMessagesPanelSizes();
            }
        });

        wrapper.add(header, BorderLayout.NORTH);
        wrapper.add(contentArea, BorderLayout.CENTER);

        return wrapper;
    }

    /**
     * Recompute max sizes of all message panels after a collapsible section toggles.
     */
    private void revalidateMessagesPanelSizes() {
        for (Component c : messagesPanel.getComponents()) {
            if (c instanceof JPanel p) {
                Dimension pref = p.getPreferredSize();
                p.setMaximumSize(new Dimension(Integer.MAX_VALUE, pref.height));
            }
        }
        messagesPanel.revalidate();
        messagesPanel.repaint();
    }

    // ── Markdown to HTML conversion ─────────────────────────────────────

    /**
     * Convert basic markdown to HTML. Handles bold, inline code, code blocks,
     * and unordered lists.
     */
    static String markdownToHtml(String markdown) {
        if (markdown == null || markdown.isEmpty()) return "";

        StringBuilder html = new StringBuilder();
        String[] lines = markdown.split("\n");
        boolean inCodeBlock = false;
        boolean inList = false;

        for (int i = 0; i < lines.length; i++) {
            String line = lines[i];

            // Code blocks: ```lang ... ```
            if (line.trim().startsWith("```")) {
                if (inCodeBlock) {
                    html.append("</pre>");
                    inCodeBlock = false;
                } else {
                    if (inList) { html.append("</ul>"); inList = false; }
                    html.append("<pre>");
                    inCodeBlock = true;
                }
                continue;
            }

            if (inCodeBlock) {
                html.append(escapeHtml(line)).append("\n");
                continue;
            }

            // Unordered list items
            if (line.matches("^\\s*[-*]\\s+.*")) {
                if (!inList) { html.append("<ul>"); inList = true; }
                String itemText = line.replaceFirst("^\\s*[-*]\\s+", "");
                html.append("<li>").append(inlineMarkdown(escapeHtml(itemText))).append("</li>");
                continue;
            } else if (inList) {
                html.append("</ul>");
                inList = false;
            }

            // Blank lines
            if (line.trim().isEmpty()) {
                html.append("<br>");
                continue;
            }

            // Headings
            if (line.startsWith("### ")) {
                html.append("<h4>").append(inlineMarkdown(escapeHtml(line.substring(4)))).append("</h4>");
            } else if (line.startsWith("## ")) {
                html.append("<h3>").append(inlineMarkdown(escapeHtml(line.substring(3)))).append("</h3>");
            } else if (line.startsWith("# ")) {
                html.append("<h2>").append(inlineMarkdown(escapeHtml(line.substring(2)))).append("</h2>");
            } else {
                // Regular paragraph text
                html.append("<p>").append(inlineMarkdown(escapeHtml(line))).append("</p>");
            }
        }

        if (inCodeBlock) html.append("</pre>");
        if (inList) html.append("</ul>");

        return html.toString();
    }

    /**
     * Handle inline markdown: **bold**, `code`.
     */
    private static String inlineMarkdown(String text) {
        // Bold: **text**
        text = text.replaceAll("\\*\\*(.+?)\\*\\*", "<b>$1</b>");
        // Inline code: `code`
        text = text.replaceAll("`([^`]+)`", "<code>$1</code>");
        return text;
    }

    private static String escapeHtml(String text) {
        return text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace("\"", "&quot;");
    }

    /**
     * Create a short summary of a tool invocation for display in the header.
     */
    private static String summarizeToolInput(String tool, Object input) {
        if (input == null) return "";
        try {
            if (input instanceof Map<?, ?> map) {
                // Common patterns: file_path, command, query, path, pattern
                for (String key : new String[]{"file_path", "command", "path", "query", "pattern", "url"}) {
                    Object val = map.get(key);
                    if (val instanceof String s && !s.isEmpty()) {
                        String truncated = s.length() > 80 ? s.substring(0, 80) + "..." : s;
                        return truncated;
                    }
                }
                // Fallback: first string value
                for (Object val : map.values()) {
                    if (val instanceof String s && !s.isEmpty()) {
                        String truncated = s.length() > 60 ? s.substring(0, 60) + "..." : s;
                        return truncated;
                    }
                }
            }
        } catch (Exception ignored) {}
        String str = input.toString();
        return str.length() > 80 ? str.substring(0, 80) + "..." : str;
    }
}
