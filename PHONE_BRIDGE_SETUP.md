# Phone Bridge Setup (Phase 6)

Talk to Raziel from your Android phone, from anywhere - and let her ring your phone, send a text
from it, open an app on it, or read its notifications aloud. This is a one-time setup across
three free/cheap tools, then it just works.

## What each tool does

| Tool | Direction | Why it's needed |
|---|---|---|
| **Tailscale** | phone → PC | Lets your phone reach your PC's local server from anywhere (not just home Wi-Fi), without exposing your PC to the whole internet. |
| **Tasker** | phone → PC | Sends a command to Raziel, or forwards a phone notification to her, as a simple HTTP request. (~$3.49 one-time; MacroDroid is a free alternative that can do the same HTTP-request job if you'd rather not pay.) |
| **Join** | PC → phone | Lets Raziel ring your phone, text from it, open an app, or set its clipboard - free app, no port needed on the phone. |

Nothing here lets anything read data OFF your phone or PC beyond exactly what you ask for.

## Step 1: Generate your token

This is the password that protects the whole bridge - anyone who has it can do anything Raziel
can do (open apps, send messages after her own yes/no, even shut the PC down), so treat it like
a real password: don't share it, don't post it anywhere public.

On the PC, open a terminal in the Voice Agent folder and run:

```
py -c "import secrets; print(secrets.token_hex(32))"
```

Copy the long string it prints. Open `config.py` and paste it in:

```python
PHONE_BRIDGE_TOKEN = "paste-your-generated-token-here"
```

Leave `PHONE_BRIDGE_ENABLED = True` and `PHONE_BRIDGE_PORT = 8765` as they are unless something
else on your PC is already using port 8765.

## Step 2: Install Tailscale on both devices

1. PC: download and install Tailscale from https://tailscale.com/download, sign in (Google
   account is easiest).
2. Phone: install "Tailscale" from the Play Store, sign in with the **same account**.
3. On the PC, open the Tailscale app/tray icon and note this PC's Tailscale IP - it looks like
   `100.x.y.z`. That's the address your phone will use to reach Raziel, from anywhere.

Tailscale keeps this connection private between your own devices - it's not a public port on
your router, and nothing else on the internet can see or reach it.

## Step 3: Restart Raziel and confirm the bridge is listening

Run `py main.py` as usual. In the console / `assistant.log` you should see:

```
Phone bridge: listening on port 8765 (reachable over Tailscale once that's set up)
```

If instead you see `Phone bridge: not starting - config.PHONE_BRIDGE_TOKEN is empty`, go back to
Step 1.

Quick test from the phone's Tailscale-connected browser (replace with your PC's Tailscale IP and
your token):

```
http://100.x.y.z:8765/ping
```

A bare page load will show `{"error": "unauthorized"}` (correct - a browser GET has no way to
send the required header), which just confirms the server is actually reachable. The real test
is the Tasker task in Step 4, which does send the header.

## Step 4: Tasker - phone → PC commands and notification forwarding

**Sending a command to Raziel** (e.g. from a home-screen shortcut, a Quick Settings tile, or
voice via Tasker's own voice-command trigger):

1. Create a new Tasker Task, e.g. "Ask Raziel".
2. Add action: **Net → HTTP Request**.
   - Method: `POST`
   - URL: `http://100.x.y.z:8765/command` (your PC's Tailscale IP)
   - Headers: `X-Raziel-Token: <your token>` and `Content-Type: application/json`
   - Body: `{"text": "%text_you_want_to_send"}` - for a fixed test, try
     `{"text": "what time is it"}` first.
3. Add a second action to show the reply, e.g. **Alert → Flash** with `%http_data` (Tasker
   parses the JSON response's `reply` field into a variable you can reference - check Tasker's
   HTTP Request action docs for the exact variable name in your version).
4. Run it once manually to confirm you get a real reply back.
5. To make it voice-driven: pair this Task with Tasker's **Profile → Event → Plugin → AutoVoice
   Recognized** (needs the free AutoVoice plugin) so saying a phrase runs it, or just put a
   shortcut to the Task on your home screen / a Quick Settings tile.

**Forwarding phone notifications to Raziel** (so she reads them aloud on the PC):

1. Create a new Tasker Profile → **Event → Notification → Notification**. Pick which apps to
   forward (e.g. WhatsApp, SMS) - forwarding everything gets noisy fast.
2. Attached Task: **Net → HTTP Request**, `POST` to `http://100.x.y.z:8765/notification`, same
   `X-Raziel-Token` header, body:
   `{"app": "%ntitle", "title": "%ntitle", "text": "%ntext"}` (Tasker's exact notification
   variable names vary slightly by version - `%ntitle`/`%ntext` are the common ones; check
   Tasker's notification-event variables if yours differ).

## Step 5: Join - PC → phone actions

1. Install "Join" from the Play Store on your phone (free).
2. Open Join, tap your phone's device card, then the menu → **"Join API"**. It shows your API
   key and this device's ID.
3. Put both in `config.py`:

```python
JOIN_API_KEY = "your-api-key-here"
JOIN_DEVICE_ID = "your-device-id-here"
```

4. Restart Raziel. Try saying: "ring my phone" - it should ring at full volume even on silent.
   Also try "text Mom I'm on my way" (if you have Mom saved in `config.CONTACTS`) or "open
   Spotify on my phone".

## What Raziel can do once this is all set up

- **From your phone, to your PC**: send any command she'd normally hear by voice - open apps,
  check reminders, ask the weather, fetch a document (see below) - and get a text reply back.
  Risky actions (sending a message, shutting down) still ask a yes/no first, exactly like
  speaking to her, just over text instead of voice.
- **Documents on demand**: `GET http://100.x.y.z:8765/file?name=resume.pdf` (with the same
  token header) downloads a file straight off your PC - it searches the same common folders
  (Desktop, Downloads, Documents, Pictures, Videos, Music) that "open the X file" already
  searches. Wire this into a Tasker task with an HTTP Request + "save response to file" action
  to make it one tap from your phone.
- **From your PC, to your phone**: "ring my phone", "text [contact] [message]" (asks yes/no
  first), "open [app] on my phone", and (via the `control_phone` tool's `open_url`/`clipboard`
  actions) open a link or set the clipboard on the phone.
- **Notifications read aloud**: whatever Tasker forwards, Raziel reads out at her desk-side
  volume, gated the same safe way a reminder is - it won't interrupt an open yes/no question or
  wake her from sleep unexpectedly.

## Honest limits, worth knowing up front

- **Join's phone actions are a small, fixed set** (ring, SMS, open app/link, set clipboard) -
  there's no "run any Android automation" button. For anything beyond that list, Join can also
  trigger a Tasker profile on the phone (via its `text` field, used as a Tasker command), which
  unlocks basically anything Tasker itself can do - but that's an advanced, optional next step,
  not part of this setup.
- **Everything shares one token.** There's no separate login per person or per device - if you
  ever want someone else's phone to use this too, they'd share your same token (not recommended)
  or you'd want to extend `phone_bridge.py` to support more than one.
- **The security model is entirely the token + Tailscale.** There's no spoken yes/no for
  phone-issued commands (nobody's in the room to say it) - possession of the token is the
  approval. Don't paste it into a place someone else could read it.
