"""Shared setup instructions, used by OnboardingWizardModal (main.py) and by
SETUP.md (see ROADMAP P1 M2). Single source of truth so the in-app wizard and
the repo docs don't drift out of sync with each other.
"""
from __future__ import annotations

GOOGLE_SETUP_STEPS = """\
GOOGLE ACCOUNT SETUP (Gmail / Calendar / Drive / Tasks)

1. Go to console.cloud.google.com and create a project (or pick an existing one).
2. APIs & Services > Enable APIs and Services — enable: Gmail API, Google
   Calendar API, Google Drive API, Tasks API. (People API too, if you want
   Contacts.)
3. APIs & Services > Google Auth Platform > Branding — fill in an app name
   and support email (Google merged the old "OAuth consent screen" into
   this "Google Auth Platform" section).
4. Google Auth Platform > Audience — add yourself as a test user. While the
   app is unpublished/"Testing", only test users can authorize it, and
   tokens expire every 7 days — that's expected for personal use.
5. Google Auth Platform > Clients > Create Client — choose "Desktop app"
   (no redirect URI to configure). Download the client secret JSON.
6. Run the local auth flow once with that client secret to mint a token
   file with Gmail/Calendar/Drive/Tasks scopes and a refresh_token.
7. Point google-tui at that token file in Settings.

This walkthrough (steps 1-6) is only needed ONCE, ever, to create the OAuth
client. If a token already exists but expired (the usual 7-day Testing-app
cap) or is missing a scope, use the "Re-authorize Google account" button
below/in Settings instead — no script to write or run.
"""

AI_PROVIDER_SETUP_STEPS = """\
AI PROVIDER SETUP (Ask pane)

Pick ONE in Settings — google-tui isn't locked into any single one, and
all of them get the same Google context (recent email/events) automatically:

- Hermes (default): needs a Nous API key. Paste it into Settings, or put
  it in ~/.hermes/config.yaml as api_key: ... Or, if you run the Hermes
  Agent CLI yourself, set "Hermes gateway URL" in Settings to a local
  `hermes proxy start` (default http://127.0.0.1:8645/v1/chat/completions)
  instead — no API key needed then. See SETUP.md.
- Claude Code: needs the `claude` CLI on your PATH, already logged in
  (run `claude` once interactively to authenticate).
- opencode: needs the `opencode` CLI on your PATH and configured
  (see opencode.ai/docs).
- Gemini CLI: needs the `gemini` CLI on your PATH, already logged in.

google-tui shells out to whichever CLI you pick with a one-shot prompt —
it doesn't need API keys for the CLI-based providers, just the binary
installed and authenticated.
"""
