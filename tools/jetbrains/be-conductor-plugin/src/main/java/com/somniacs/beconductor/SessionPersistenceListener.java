package com.somniacs.beconductor;

import com.intellij.openapi.application.ApplicationManager;
import com.intellij.openapi.diagnostic.Logger;
import com.intellij.openapi.project.Project;
import com.intellij.openapi.project.ProjectManagerListener;
import com.somniacs.beconductor.api.ApiModels;
import com.somniacs.beconductor.api.BeConductorClient;
import com.somniacs.beconductor.toolwindow.SessionListPanel;
import org.jetbrains.annotations.NotNull;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * Gracefully stops tracked be-conductor sessions when an individual project closes.
 * <p>
 * During IDE shutdown, {@link AppShutdownListener} handles this via
 * {@code appClosing()} which fires before any disposal. This listener
 * covers the case where a single project is closed while the IDE stays open.
 * If sessions were already stopped by AppShutdownListener, this is a no-op
 * (the running-session filter finds nothing).
 */
public class SessionPersistenceListener implements ProjectManagerListener {

    private static final Logger LOG = Logger.getInstance(SessionPersistenceListener.class);

    @Override
    public void projectClosing(@NotNull Project project) {
        // During IDE shutdown, AppShutdownListener.appClosing() already handled this.
        // Skip to avoid redundant work and potential service-disposal issues.
        if (ApplicationManager.getApplication().isDisposed()) return;

        List<String> tracked = new ArrayList<>(SessionListPanel.getTrackedSessions(project));
        if (tracked.isEmpty()) return;

        try {
            BeConductorClient client = BeConductorClient.getInstance();
            if (!client.isServerRunning()) return;

            List<ApiModels.SessionResponse> sessions = client.listSessions();
            List<ApiModels.SessionResponse> running = new ArrayList<>();
            for (ApiModels.SessionResponse s : sessions) {
                if (tracked.contains(s.name) && "running".equals(s.status)) {
                    running.add(s);
                }
            }

            if (running.isEmpty()) return;

            // Save which sessions were running so auto-resume only picks up these
            SessionListPanel.setRunningAtClose(project,
                    running.stream().map(s -> s.name).collect(java.util.stream.Collectors.toList()));

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
            LOG.info("be-conductor: gracefully stopped " + running.size() + " session(s) on project close");
        } catch (Exception e) {
            LOG.info("be-conductor: session persistence on close skipped: " + e.getMessage());
        }
    }
}
