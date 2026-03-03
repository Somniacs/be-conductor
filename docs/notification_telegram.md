# Telegram Notifications

Get notified on your phone when an agent session needs your attention.

## 1. Create a Telegram Bot

1. Open Telegram and search for **@BotFather**
2. Tap **Start**, then send `/newbot`
3. Enter a name for your bot (e.g. `Be-Conductor`)
4. Enter a username ending in `bot` (e.g. `be_conductor_myname_bot`)
5. BotFather replies with a **token** — a long string like:
   ```
   123456789:ABCdefGHI-jklMNOpqrSTUvwxYZ
   ```
6. Copy the token

## 2. Get Your Chat ID

1. In Telegram, search for **@userinfobot** (the one called `@userinfo3bot` with ~57k users)
2. Tap **Start**
3. It replies with your info — note the **Id** number (e.g. `123456789`)

## 3. Start a Conversation with Your Bot

This step is required — Telegram bots can only message users who have started a conversation first.

1. Search for the bot username you just created (e.g. `@be_conductor_myname_bot`)
2. Tap **Start**

## 4. Configure in Be-Conductor

1. Open the Be-Conductor dashboard
2. Tap the gear icon to open **Settings**
3. Go to the **Notifications** tab
4. Under **Webhook**:
   - Enable the **webhook toggle**
   - Platform should be set to **Telegram**
   - Paste your **Bot Token** from step 1
   - Paste your **Chat ID** from step 2
5. Tap **Test webhook**
6. You should receive a message from your bot in Telegram

## How It Works

When an agent session is idle and waiting for input (e.g. asking a question, waiting for confirmation), Be-Conductor sends a message to your Telegram bot. This works from any device and any browser — no push notification support required.

Notifications are only sent when:
- The session has been idle for a few seconds
- The terminal output matches a known attention pattern (question prompt, error, permission request, etc.)

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "chat not found" | Open your bot in Telegram and tap **Start** first (step 3) |
| "Unauthorized" | Check that the bot token is correct — copy it again from @BotFather |
| "Bad Request" | Make sure the Chat ID is a number, not a username |
| No message received | Verify the webhook toggle is enabled and both fields are filled in |

## Group Notifications (Optional)

To send notifications to a Telegram group instead of a private chat:

1. Add your bot to the group
2. Send a message in the group
3. Get the group chat ID by visiting:
   ```
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```
   Look for `"chat":{"id":-100...}` — the negative number is the group chat ID
4. Use that group chat ID in the **Chat ID** field
