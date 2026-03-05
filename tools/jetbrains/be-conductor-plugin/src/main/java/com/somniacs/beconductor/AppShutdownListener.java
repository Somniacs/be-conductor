package com.somniacs.beconductor;

import com.intellij.ide.AppLifecycleListener;
import com.intellij.openapi.diagnostic.Logger;
import com.intellij.openapi.project.Project;
import com.intellij.openapi.project.ProjectManager;
import com.somniacs.beconductor.api.ApiModels;
import com.somniacs.beconductor.api.BeConductorClient;
import com.somniacs.beconductor.toolwindow.SessionListPanel;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * Gracefully stops tracked be-conductor sessions when the IDE shuts down.
 * <p>
 * This uses {@link AppLifecycleListener#appClosing()} which fires early in the
 * shutdown sequence — before any project or service disposal — ensuring all
 * services are still available. The companion {@link SessionPersistenceListener}
 * handles individual project close (without IDE shutdown).
 */
public class AppShutdownListener implements AppLifecycleListener {

    private static final Logger LOG = Logger.getInstance(AppShutdownListener.class);

    @Override
    public void appClosing() {
        // Collect tracked sessions from all open projects
        Set<String> allTracked = new LinkedHashSet<>();
        for (Project project : ProjectManager.getInstance().getOpenProjects()) {
            allTracked.addAll(SessionListPanel.getTrackedSessions(project));
        }

        if (allTracked.isEmpty()) return;

        try {
            BeConductorClient client = BeConductorClient.getInstance();
            if (!client.isServerRunning()) return;

            List<ApiModels.SessionResponse> sessions = client.listSessions();
            List<ApiModels.SessionResponse> running = new ArrayList<>();
            for (ApiModels.SessionResponse s : sessions) {
                if (allTracked.contains(s.name) && "running".equals(s.status)) {
                    running.add(s);
                }
            }

            if (running.isEmpty()) return;

            // Save which sessions were running so auto-resume only picks up these
            List<String> runningNames = new ArrayList<>();
            for (ApiModels.SessionResponse s : running) runningNames.add(s.name);
            for (Project project : ProjectManager.getInstance().getOpenProjects()) {
                SessionListPanel.setRunningAtClose(project, runningNames);
            }

            // Fire parallel graceful stops
            CountDownLatch latch = new CountDownLatch(running.size());
            for (ApiModels.SessionResponse s : running) {
                new Thread(() -> {
                    try {
                        client.stopSession(s.id, "graceful");
                    } catch (Exception e) {
                        LOG.info("be-conductor: failed to stop session " + s.name + ": " + e.getMessage());
                    } finally {
                        latch.countDown();
                    }
                }).start();
            }

            // Wait up to 3s for graceful stops to fire (resume tokens need time)
            latch.await(3, TimeUnit.SECONDS);
            LOG.info("be-conductor: gracefully stopped " + running.size() + " session(s) on IDE shutdown");
        } catch (Exception e) {
            LOG.info("be-conductor: session persistence on shutdown skipped: " + e.getMessage());
        }
    }
}
