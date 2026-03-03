# Slack Notifications

Get notified in a Slack channel when an agent session needs your attention.

## 1. Create a Slack App with Incoming Webhook

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and click **Create New App**
2. Choose **From scratch**
3. Name it (e.g. `Be-Conductor`) and pick the workspace you want to post to
4. Click **Create App**

## 2. Create a Channel (Optional)

Create a dedicated channel for notifications (e.g. `#be-conductor`) or use an existing one. You can also send notifications to a DM — see [DM Notifications](#dm-notifications-alternative) below.

## 3. Enable Incoming Webhooks

1. In your app's settings page, click **Incoming Webhooks** in the left sidebar
2. Toggle **Activate Incoming Webhooks** to **On**
3. Click **Add New Webhook to Workspace** at the bottom
4. Slack asks you to pick a channel — select the channel you created (or any existing channel). Each webhook is tied to exactly one channel
5. Click **Allow**
6. Copy the **Webhook URL** — it looks like:
   ```
   https://hooks.slack.com/services/T.../B.../...
   ```

## 4. Configure in Be-Conductor

1. Open the Be-Conductor dashboard
2. Tap the gear icon to open **Settings**
3. Go to the **Notifications** tab
4. Under **Webhook**:
   - Enable the **webhook toggle**
   - Set Platform to **Slack**
   - Paste your **Webhook URL** from step 3
5. Tap **Test webhook**
6. You should see a test message in your Slack channel

## How It Works

When an agent session is idle and waiting for input (e.g. asking a question, waiting for confirmation), Be-Conductor posts a message to your Slack channel. Messages include the session name, what the agent is waiting for, and a snippet of the terminal output.

Example message:

> :bell: **research**: Needs confirmation
> ```May I edit src/main.py?```

Notifications are only sent when:
- The session has been idle for a few seconds
- The terminal output matches a known attention pattern (question prompt, error, permission request, etc.)

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "channel_not_found" or 404 | The webhook URL may have been revoked. Generate a new one in your app settings |
| "invalid_payload" | Make sure you pasted the full URL starting with `https://hooks.slack.com/services/` |
| No message received | Verify the webhook toggle is enabled and the URL field is filled in |
| Messages go to wrong channel | Each webhook URL is tied to a specific channel. Create a new webhook for a different channel |
| Test works but no real notifications | Make sure an agent session is actually idle and waiting — try asking it a question and walking away |

## DM Notifications (Alternative)

To receive Slack notifications as a direct message instead of in a channel:

1. When adding the webhook to your workspace (step 3.4), choose your own name instead of a channel
2. The webhook will post to your Slackbot DM

## Multiple Channels

Be-Conductor supports one global webhook. If you need notifications in multiple channels, use a Slack Workflow that forwards the message, or set up a custom webhook endpoint that relays to multiple Slack webhooks.
