package com.somniacs.beconductor;

import com.intellij.ide.AppLifecycleListener;
import com.intellij.ide.util.PropertiesComponent;
import com.intellij.openapi.diagnostic.Logger;
import com.intellij.openapi.updateSettings.impl.UpdateSettings;

import java.util.List;

/**
 * Wires the be-conductor plugin into the IDE's built-in update mechanism
 * so users who installed the plugin from a downloaded .zip (instead of
 * from the JetBrains Marketplace) also get upgrade prompts — matching
 * the VS Code extension, which has its own GitHub-backed update check.
 *
 * <p>The approach is deliberately minimal: on IDE startup, register our
 * public {@code updatePlugins.xml} feed as a "custom plugin repository"
 * via {@link UpdateSettings#getStoredPluginHosts()}. The IDE then polls
 * it on its normal schedule (Settings → Plugins → Gear → Manage Plugin
 * Repositories) and surfaces any newer version through the platform's
 * standard update notification — including download, install-on-next-
 * restart, and the restart prompt. We do not need to implement any of
 * that ourselves.
 *
 * <p>Registration is guarded by a one-shot {@link PropertiesComponent}
 * flag so we don't re-add the URL if the user explicitly removed it.
 */
public class PluginUpdateChecker implements AppLifecycleListener {

    private static final Logger LOG = Logger.getInstance(PluginUpdateChecker.class);

    /** The raw GitHub URL of {@code tools/jetbrains/updatePlugins.xml}
     *  on the master branch. The release workflow commits updates to
     *  that file so this URL always advertises the latest released
     *  plugin version.  */
    private static final String UPDATE_FEED_URL =
            "https://raw.githubusercontent.com/somniacs/be-conductor/master/tools/jetbrains/updatePlugins.xml";

    /** Stable key so we can tell "the user removed our repo" apart from
     *  "we never added it." */
    private static final String REGISTERED_KEY = "be-conductor.update.feedRegistered";

    @Override
    public void appStarted() {
        try {
            ensureRepositoryRegistered();
        } catch (Throwable t) {
            // Best-effort. Never block IDE startup on this.
            LOG.info("be-conductor: plugin repository registration skipped: " + t.getMessage());
        }
    }

    private void ensureRepositoryRegistered() {
        PropertiesComponent props = PropertiesComponent.getInstance();
        boolean alreadyAttempted = props.getBoolean(REGISTERED_KEY, false);

        UpdateSettings settings = UpdateSettings.getInstance();
        List<String> hosts = settings.getStoredPluginHosts();

        if (hosts.contains(UPDATE_FEED_URL)) {
            // Already registered — just record the flag so a future
            // removal by the user is recognized.
            props.setValue(REGISTERED_KEY, true);
            return;
        }

        if (alreadyAttempted) {
            // We added it in a previous session and the user has since
            // removed it. Respect that — don't re-add every startup.
            return;
        }

        hosts.add(UPDATE_FEED_URL);
        props.setValue(REGISTERED_KEY, true);
        LOG.info("be-conductor: registered plugin update feed " + UPDATE_FEED_URL);
    }
}
