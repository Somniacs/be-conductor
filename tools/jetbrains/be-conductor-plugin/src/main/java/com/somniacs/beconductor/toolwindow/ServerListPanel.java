package com.somniacs.beconductor.toolwindow;

import com.intellij.icons.AllIcons;
import com.intellij.notification.Notification;
import com.intellij.notification.NotificationType;
import com.intellij.notification.Notifications;
import com.intellij.openapi.application.ApplicationManager;
import com.intellij.openapi.project.Project;
import com.intellij.ui.SimpleColoredComponent;
import com.intellij.ui.SimpleTextAttributes;
import com.intellij.ui.components.JBList;
import com.intellij.ui.components.JBScrollPane;
import com.intellij.util.Alarm;
import com.somniacs.beconductor.api.ApiModels;
import com.somniacs.beconductor.api.BeConductorClient;
import com.somniacs.beconductor.api.ServerRegistry;

import javax.swing.*;
import java.awt.*;
import java.awt.event.MouseAdapter;
import java.awt.event.MouseEvent;
import java.util.*;
import java.util.List;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Panel for managing be-conductor server connections.
 * Shows the server list with online/offline status and provides
 * add/remove/probe actions + Tailscale peer discovery.
 */
public class ServerListPanel extends JPanel {

    private static final int PROBE_INTERVAL_MS = 30000;

    private final Project project;
    private final DefaultListModel<ServerRegistry.Server> listModel;
    private final JBList<ServerRegistry.Server> serverList;
    private final JLabel statusLabel;
    private final Alarm probeAlarm;

    /** Cached probe results: serverKey -> InfoResponse (null = offline). */
    private final Map<String, ApiModels.InfoResponse> probeCache = new ConcurrentHashMap<>();

    // Tailscale discovery UI
    private JPanel tailscalePanel;
    private DefaultListModel<ApiModels.TailscalePeer> peerModel;

    public ServerListPanel(Project project) {
        super(new BorderLayout());
        this.project = project;

        // === Server list (created first so toolbar lambdas can reference it) ===
        listModel = new DefaultListModel<>();
        serverList = new JBList<>(listModel);
        serverList.setCellRenderer(new ServerCellRenderer());
        serverList.setSelectionMode(ListSelectionModel.SINGLE_SELECTION);

        // === Toolbar ===
        JPanel toolbar = new JPanel();
        toolbar.setLayout(new BoxLayout(toolbar, BoxLayout.Y_AXIS));

        JPanel actions = new JPanel(new FlowLayout(FlowLayout.LEFT, 2, 1));

        JButton addBtn = createToolbarButton("Add", AllIcons.General.Add, "Add a remote server");
        addBtn.addActionListener(e -> addServer());
        actions.add(addBtn);

        JButton removeBtn = createToolbarButton("Remove", AllIcons.General.Remove, "Remove selected server");
        removeBtn.addActionListener(e -> {
            ServerRegistry.Server s = serverList.getSelectedValue();
            if (s != null && !s.isLocal()) removeServer(s);
        });
        actions.add(removeBtn);

        JButton probeBtn = createToolbarButton("Test", AllIcons.Actions.Refresh, "Test connection to all servers");
        probeBtn.addActionListener(e -> probeAll());
        actions.add(probeBtn);

        toolbar.add(actions);
        add(toolbar, BorderLayout.NORTH);

        serverList.addMouseListener(new MouseAdapter() {
            @Override
            public void mouseClicked(MouseEvent e) {
                if (e.getClickCount() == 2) {
                    ServerRegistry.Server s = serverList.getSelectedValue();
                    if (s != null && !s.isLocal()) toggleEnabled(s);
                }
            }
            @Override
            public void mousePressed(MouseEvent e) { showPopup(e); }
            @Override
            public void mouseReleased(MouseEvent e) { showPopup(e); }
            private void showPopup(MouseEvent e) {
                if (!e.isPopupTrigger()) return;
                int idx = serverList.locationToIndex(e.getPoint());
                if (idx < 0) return;
                serverList.setSelectedIndex(idx);
                ServerRegistry.Server s = listModel.getElementAt(idx);
                createContextMenu(s).show(serverList, e.getX(), e.getY());
            }
        });

        add(new JBScrollPane(serverList), BorderLayout.CENTER);

        // === Bottom: Tailscale + status ===
        JPanel bottom = new JPanel(new BorderLayout());

        // Tailscale discovery
        tailscalePanel = createTailscalePanel();
        bottom.add(tailscalePanel, BorderLayout.CENTER);

        statusLabel = new JLabel(" ");
        statusLabel.setFont(statusLabel.getFont().deriveFont(11f));
        statusLabel.setForeground(new Color(0x88, 0x88, 0xaa));
        statusLabel.setBorder(BorderFactory.createEmptyBorder(2, 6, 2, 6));
        bottom.add(statusLabel, BorderLayout.SOUTH);

        add(bottom, BorderLayout.SOUTH);

        // === Initial load + periodic probe ===
        refreshList();
        probeAlarm = new Alarm(Alarm.ThreadToUse.POOLED_THREAD, project);
        probeAll();
        scheduleProbe();
    }

    private void scheduleProbe() {
        if (project.isDisposed()) return;
        probeAlarm.addRequest(() -> {
            probeAll();
            scheduleProbe();
        }, PROBE_INTERVAL_MS);
    }

    // === List management ===

    public void refreshList() {
        List<ServerRegistry.Server> servers = ServerRegistry.getInstance().getServers();
        SwingUtilities.invokeLater(() -> {
            ServerRegistry.Server prev = serverList.getSelectedValue();
            listModel.clear();
            int selectIdx = -1;
            for (int i = 0; i < servers.size(); i++) {
                listModel.addElement(servers.get(i));
                if (prev != null && servers.get(i).key.equals(prev.key)) selectIdx = i;
            }
            if (selectIdx >= 0) serverList.setSelectedIndex(selectIdx);
            statusLabel.setText(servers.size() + " server(s)");
        });
    }

    // === Probe ===

    private void probeAll() {
        List<ServerRegistry.Server> servers = ServerRegistry.getInstance().getServers();
        for (ServerRegistry.Server server : servers) {
            ApplicationManager.getApplication().executeOnPooledThread(() -> {
                try {
                    ApiModels.InfoResponse info = BeConductorClient.getInstance().getInfo(server.key);
                    probeCache.put(server.key, info);
                } catch (Exception e) {
                    probeCache.remove(server.key);
                }
                SwingUtilities.invokeLater(() -> serverList.repaint());
            });
        }
    }

    // === Actions ===

    private void addServer() {
        JPanel panel = new JPanel(new GridLayout(2, 2, 4, 4));
        JTextField urlField = new JTextField("", 20);
        JTextField labelField = new JTextField("", 20);
        panel.add(new JLabel("URL:"));
        panel.add(urlField);
        panel.add(new JLabel("Label:"));
        panel.add(labelField);

        int result = JOptionPane.showConfirmDialog(this, panel, "Add Server",
                JOptionPane.OK_CANCEL_OPTION, JOptionPane.PLAIN_MESSAGE);
        if (result != JOptionPane.OK_OPTION) return;

        String url = urlField.getText().trim();
        if (url.isEmpty()) return;
        String label = labelField.getText().trim();

        ApplicationManager.getApplication().executeOnPooledThread(() -> {
            // Probe to get hostname
            try {
                ApiModels.InfoResponse info = BeConductorClient.getInstance().getInfo(
                        url.replaceFirst("^https?://", "").replaceAll("/+$", ""));
                // Not added yet — use the URL directly for probing
                // Actually, let's just use a temp probe
            } catch (Exception ignored) {}

            ServerRegistry.Server server = ServerRegistry.getInstance().addServer(url,
                    label.isEmpty() ? null : label);
            if (server == null) {
                SwingUtilities.invokeLater(() ->
                        JOptionPane.showMessageDialog(this, "Server already exists.", "Add Server",
                                JOptionPane.WARNING_MESSAGE));
                return;
            }
            SwingUtilities.invokeLater(() -> {
                refreshList();
                probeAll();
                BeConductorToolWindowFactory.refreshAll(project);
            });
        });
    }

    private void removeServer(ServerRegistry.Server server) {
        int confirm = JOptionPane.showConfirmDialog(this,
                "Remove server \"" + server.label + "\"?", "Remove Server",
                JOptionPane.YES_NO_OPTION, JOptionPane.WARNING_MESSAGE);
        if (confirm != JOptionPane.YES_OPTION) return;

        ServerRegistry.getInstance().removeServer(server.key);
        probeCache.remove(server.key);
        refreshList();
        BeConductorToolWindowFactory.refreshAll(project);
    }

    private void toggleEnabled(ServerRegistry.Server server) {
        ServerRegistry.getInstance().setEnabled(server.key, !server.enabled);
        refreshList();
        BeConductorToolWindowFactory.refreshAll(project);
    }

    private void renameServer(ServerRegistry.Server server) {
        String newLabel = JOptionPane.showInputDialog(this, "New label:", server.label);
        if (newLabel != null && !newLabel.trim().isEmpty()) {
            ServerRegistry.getInstance().setLabel(server.key, newLabel.trim());
            refreshList();
        }
    }

    // === Context menu ===

    private JPopupMenu createContextMenu(ServerRegistry.Server server) {
        JPopupMenu menu = new JPopupMenu();

        if (!server.isLocal()) {
            JMenuItem toggleItem = new JMenuItem(server.enabled ? "Disable" : "Enable");
            toggleItem.addActionListener(e -> toggleEnabled(server));
            menu.add(toggleItem);

            JMenuItem renameItem = new JMenuItem("Rename...");
            renameItem.addActionListener(e -> renameServer(server));
            menu.add(renameItem);

            menu.addSeparator();

            JMenuItem removeItem = new JMenuItem("Remove");
            removeItem.addActionListener(e -> removeServer(server));
            menu.add(removeItem);
        }

        JMenuItem probeItem = new JMenuItem("Test Connection");
        probeItem.addActionListener(e -> {
            ApplicationManager.getApplication().executeOnPooledThread(() -> {
                try {
                    ApiModels.InfoResponse info = BeConductorClient.getInstance().getInfo(server.key);
                    probeCache.put(server.key, info);
                    SwingUtilities.invokeLater(() -> {
                        serverList.repaint();
                        Notifications.Bus.notify(new Notification("be-conductor", "Server Online",
                                server.label + " — v" + info.version, NotificationType.INFORMATION));
                    });
                } catch (Exception ex) {
                    probeCache.remove(server.key);
                    SwingUtilities.invokeLater(() -> {
                        serverList.repaint();
                        Notifications.Bus.notify(new Notification("be-conductor", "Server Offline",
                                server.label + " — " + ex.getMessage(), NotificationType.WARNING));
                    });
                }
            });
        });
        menu.add(probeItem);

        return menu;
    }

    // === Tailscale discovery panel ===

    private JPanel createTailscalePanel() {
        JPanel panel = new JPanel(new BorderLayout());
        panel.setBorder(BorderFactory.createTitledBorder("Tailscale Discovery"));

        peerModel = new DefaultListModel<>();
        JBList<ApiModels.TailscalePeer> peerList = new JBList<>(peerModel);
        peerList.setCellRenderer(new DefaultListCellRenderer() {
            @Override
            public Component getListCellRendererComponent(JList<?> list, Object value, int index,
                                                           boolean isSelected, boolean cellHasFocus) {
                super.getListCellRendererComponent(list, value, index, isSelected, cellHasFocus);
                if (value instanceof ApiModels.TailscalePeer peer) {
                    String label = peer.hostname;
                    if (label == null || label.isEmpty()) label = peer.dns_name;
                    setText(label + (peer.online ? "" : " (offline)"));
                    setIcon(peer.online ? AllIcons.RunConfigurations.TestPassed : AllIcons.RunConfigurations.TestIgnored);
                }
                return this;
            }
        });
        peerList.setVisibleRowCount(3);
        panel.add(new JBScrollPane(peerList), BorderLayout.CENTER);

        JPanel btnPanel = new JPanel(new FlowLayout(FlowLayout.LEFT, 4, 2));
        JButton scanBtn = new JButton("Scan");
        scanBtn.addActionListener(e -> scanTailscale());
        btnPanel.add(scanBtn);

        JButton addPeerBtn = new JButton("Add Selected");
        addPeerBtn.addActionListener(e -> {
            ApiModels.TailscalePeer peer = peerList.getSelectedValue();
            if (peer == null) return;
            String url = "http://" + peer.ip + ":7777";
            String label = peer.hostname != null ? peer.hostname : peer.ip;
            ServerRegistry.Server added = ServerRegistry.getInstance().addServer(url, label);
            if (added != null) {
                refreshList();
                probeAll();
                BeConductorToolWindowFactory.refreshAll(project);
                // Remove from peer list
                peerModel.removeElement(peer);
            }
        });
        btnPanel.add(addPeerBtn);
        panel.add(btnPanel, BorderLayout.SOUTH);

        return panel;
    }

    private void scanTailscale() {
        ApplicationManager.getApplication().executeOnPooledThread(() -> {
            try {
                List<ApiModels.TailscalePeer> peers = BeConductorClient.getInstance()
                        .getTailscalePeers("local");
                // Filter out already-added servers
                Set<String> existingIps = new HashSet<>();
                for (ServerRegistry.Server s : ServerRegistry.getInstance().getServers()) {
                    if (s.url != null) {
                        existingIps.add(s.url.replaceFirst("^https?://", "").replaceAll(":\\d+$", ""));
                    }
                }
                List<ApiModels.TailscalePeer> available = peers.stream()
                        .filter(p -> !existingIps.contains(p.ip))
                        .toList();
                SwingUtilities.invokeLater(() -> {
                    peerModel.clear();
                    for (ApiModels.TailscalePeer p : available) peerModel.addElement(p);
                    if (available.isEmpty()) {
                        statusLabel.setText("No new Tailscale peers found");
                    } else {
                        statusLabel.setText(available.size() + " peer(s) found");
                    }
                });
            } catch (Exception e) {
                SwingUtilities.invokeLater(() ->
                        statusLabel.setText("Tailscale scan failed: " + e.getMessage()));
            }
        });
    }

    // === Cell renderer ===

    private class ServerCellRenderer extends DefaultListCellRenderer {
        @Override
        public Component getListCellRendererComponent(JList<?> list, Object value, int index,
                                                       boolean isSelected, boolean cellHasFocus) {
            if (!(value instanceof ServerRegistry.Server server)) {
                return super.getListCellRendererComponent(list, value, index, isSelected, cellHasFocus);
            }

            SimpleColoredComponent component = new SimpleColoredComponent();
            component.setOpaque(true);
            if (isSelected) {
                component.setBackground(list.getSelectionBackground());
                component.setForeground(list.getSelectionForeground());
            } else {
                component.setBackground(list.getBackground());
                component.setForeground(list.getForeground());
            }

            // Online/offline icon
            ApiModels.InfoResponse info = probeCache.get(server.key);
            boolean online = info != null;
            if (online) {
                component.setIcon(AllIcons.RunConfigurations.TestPassed);
            } else if (!server.enabled) {
                component.setIcon(AllIcons.RunConfigurations.TestIgnored);
            } else {
                component.setIcon(AllIcons.RunConfigurations.TestError);
            }

            // Label
            SimpleTextAttributes labelAttr = server.enabled
                    ? SimpleTextAttributes.REGULAR_BOLD_ATTRIBUTES
                    : SimpleTextAttributes.GRAYED_BOLD_ATTRIBUTES;
            component.append(server.label, labelAttr);

            // URL
            String urlDisplay = server.isLocal() ? "localhost:7777" : (server.url != null ? server.url : server.key);
            component.append("  " + urlDisplay, SimpleTextAttributes.GRAYED_ATTRIBUTES);

            // Version
            if (info != null && info.version != null) {
                component.append("  v" + info.version, new SimpleTextAttributes(
                        SimpleTextAttributes.STYLE_ITALIC, new Color(0x44, 0xbb, 0x77)));
            }

            // Disabled badge
            if (!server.enabled) {
                component.append("  [disabled]", new SimpleTextAttributes(
                        SimpleTextAttributes.STYLE_ITALIC, new Color(0xaa, 0x88, 0x44)));
            }

            return component;
        }
    }

    // === Utility ===

    private static JButton createToolbarButton(String text, Icon icon, String tooltip) {
        JButton btn = new JButton(text, icon);
        btn.setToolTipText(tooltip);
        btn.setMargin(new Insets(2, 6, 2, 6));
        btn.setFont(btn.getFont().deriveFont(11f));
        return btn;
    }
}
