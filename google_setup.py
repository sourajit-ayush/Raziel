"""
google_setup.py - connect Raziel to your Google account. Run it ONCE:

    py google_setup.py

It needs the OAuth client file `google_credentials.json` in the Voice Agent folder (see the steps it prints
if the file is missing), opens your browser for the Google sign-in, saves the token to `google_token.json`
and prints the Gmail address it signed in as. After that Raziel can:

  * read your calendar and add events (she always asks "yes or no" first),
  * read your unread emails (read-only: she can never send, change or delete mail).

Options:  --check   only test the saved sign-in (no browser), print the address
Nothing is sent anywhere except to Google. The token file stays on this computer (owner-only where possible).
"""

from __future__ import annotations

import os
import sys
import webbrowser
from typing import List, Optional

import google_api

STEPS = """
I could not find the Google credentials file:
    {path}

You need to create a free Google "OAuth client" once (about 5 minutes):

  1. Open https://console.cloud.google.com/ , sign in, and create a project (any name, e.g. "Raziel").
  2. APIs & Services -> Library: search for and ENABLE both
       - "Google Calendar API"
       - "Gmail API"
  3. Open "Google Auth platform" (or "APIs & Services -> OAuth consent screen") and set it up:
       - Branding: app name (e.g. Raziel) and your support email.
       - Audience: choose "External".
           * Either leave it in "Testing" and add your own Gmail address under "Test users".
             (In Testing mode Google signs you out every 7 days - just run this script again then.)
           * Or press "Publish app" so the sign-in never expires. You will see an "unverified app"
             warning when signing in; because it is your own app you can click "Advanced" ->
             "Go to Raziel (unsafe)".
       - Data Access: "Add or remove scopes" and add these two:
           https://www.googleapis.com/auth/calendar.events
           https://www.googleapis.com/auth/gmail.readonly
  4. Clients -> "Create client" -> Application type: "Desktop app" -> Create -> "Download JSON".
  5. Save that file as  google_credentials.json  in this folder:
       {folder}

Then run this again:   py google_setup.py
"""


def _say(text: str = "") -> None:
    try:
        print(text)
    except UnicodeEncodeError:                     # a legacy Windows console
        print(text.encode("ascii", "replace").decode("ascii"))


def _open_and_print(url: str):
    """Opens the browser and also prints the link, in case no browser can be started."""
    _say("Opening your browser for the Google sign-in...")
    _say("If nothing opens, copy this link into your browser:")
    _say("  " + url)
    try:
        return webbrowser.open(url)
    except Exception:  # noqa: BLE001
        return False


def _describe_error(e: Exception) -> str:
    return str(e) or e.__class__.__name__


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    check_only = "--check" in argv

    _say("Raziel - Google setup")
    _say("=" * 21)

    path = google_api.credentials_path()
    if not google_api.is_configured():
        _say(STEPS.format(path=path, folder=os.path.dirname(os.path.abspath(path))))
        return 1

    try:
        google_api._load_credentials()             # validates the JSON (Desktop app client?)
    except google_api.GoogleError as e:
        _say(f"Problem with {path}:")
        _say("  " + _describe_error(e))
        return 1

    if check_only:
        if not google_api.is_connected():
            _say("Not signed in yet. Run:  py google_setup.py")
            return 1
        try:
            email = google_api.get_profile_email()
        except google_api.GoogleError as e:
            _say("The saved sign-in does not work: " + _describe_error(e))
            return 1
        _say(f"Signed in as {email or '(unknown address)'}. Everything is fine.")
        return 0

    if google_api.is_connected():
        _say("There is already a saved sign-in - it will be replaced.")
    _say("A browser window will open. Sign in with your Google account and click Allow.")
    _say('If Google says "hasn\'t verified this app", click Advanced -> Go to Raziel (unsafe): it is your own app.')
    _say("Please tick BOTH permissions (calendar events and reading your email).")
    _say()

    try:
        tok = google_api.authorize_interactive(open_browser=_open_and_print, timeout=180)
    except google_api.GoogleError as e:
        _say()
        _say("Sign-in did not finish: " + _describe_error(e))
        return 1
    except KeyboardInterrupt:
        _say()
        _say("Cancelled.")
        return 1

    missing = google_api.missing_scopes(tok)
    if missing:
        _say()
        _say("Warning: you did not allow everything. Missing permission(s):")
        for scope in missing:
            _say("   " + scope)
        _say("Run this script again and tick all the boxes if you want every feature.")

    try:
        email = google_api.get_profile_email()
    except google_api.GoogleError as e:
        email = ""
        _say()
        _say("Signed in, but I couldn't read your Gmail address: " + _describe_error(e))
        _say("(Is the Gmail API enabled in your Google Cloud project?)")

    _say()
    if email:
        _say(f"Signed in as {email}")
    _say("All set. Raziel can now read your calendar, add events (after asking you yes or no)")
    _say("and read your unread emails (never send). Try:  \"what's on my calendar today?\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
