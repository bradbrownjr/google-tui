# SETUP.md — Google Cloud Console walkthrough for google-tui

This is the full walkthrough. The in-app onboarding wizard
(`google_tui/setup_instructions.py`) shows a condensed version of the same
steps when it detects nothing is configured yet — this file is the
detailed version, plus the "why" behind each step.

## 1. Create a Google Cloud project

1. Go to [console.cloud.google.com](https://console.cloud.google.com/) and
   sign in with the Google account you want google-tui to access.
2. Top bar → project picker → **New Project**. Any name is fine (e.g.
   "google-tui"). You don't need an Organization.

Everything below happens inside that project — check the project picker
at the top of the console if something looks missing; it's a common trap
to end up creating credentials in the wrong project.

## 2. Enable the APIs you want google-tui to use

Left sidebar → **APIs & Services** → **Enabled APIs & services** → **+
Enable APIs and Services**, then search for and enable each of these:

| API | Used for |
|---|---|
| **Gmail API** | Mail tab |
| **Google Calendar API** | Calendar tab |
| **Google Drive API** | Drive tab |
| **Tasks API** | Tasks pane |
| **People API** | Contacts tab (planned — see ROADMAP) |
| **Routes API** | Navigation tab (driving directions; this one needs billing, see §6) |

All of these are free within Google's normal per-user quotas except
Routes API — see §6 before enabling it.

## 3. Configure the OAuth branding (Google Auth Platform)

Google folded what used to be called the "OAuth consent screen" into a
section now named **Google Auth Platform**. If you've used an older
Google Cloud tutorial, this is the same place under a new name.

1. **APIs & Services** → **Google Auth Platform** → **Branding**. If you
   see "Google Auth Platform not configured yet", click **Get Started**.
2. App name: anything (e.g. "google-tui"). Support email: your own email
   is fine for a personal tool.
3. User type: **External** (Internal requires a Google Workspace
   organization — most personal Gmail accounts don't have one available).
4. Save through the wizard; you don't need to fill in scopes here for a
   personal tool — they're requested at auth time instead.

## 4. Add yourself as a test user

**Google Auth Platform** → **Audience**:

- Publishing status: leave as **Testing**.
- Under **Test users**, add your own Google account's email address.

**What this means in practice:** while the app is in Testing status, only
the test users you list can complete the OAuth flow, and Google expires
each test user's authorization (and the refresh token that comes with it)
**7 days** after they consent. For a personal single-user CLI tool this
is the normal, expected setup — you'll just need to re-run the local auth
flow (step 6) roughly weekly, or whenever google-tui reports an expired
token. Switching to "In production" removes the 7-day cap, but Gmail's
scopes are considered "restricted," so publishing may prompt Google's
formal verification review — usually more overhead than it's worth for a
tool only you use.

## 5. Create an OAuth client

**Google Auth Platform** → **Clients** → **Create Client**:

- Application type: **Desktop app**. (Desktop apps don't need a redirect
  URI configured — the simplest option for a local CLI/TUI tool.)
- Name: anything.
- Click **Create**, then **Download JSON** — this is your client secret
  file. Keep it out of version control (it's already covered by
  `.gitignore`'s `*.json.bak`/token patterns — double check before
  committing anything from this step).

## 6. (Optional) Routes API and billing

Routes API — used by the Navigation tab — is part of **Google Maps
Platform**, which is billed, unlike the Workspace APIs above. If you want
driving directions:

1. **Billing** (left sidebar) → link a billing account to this project.
   Google Maps Platform has a recurring free monthly credit that covers
   light personal use, but a billing account must be attached regardless.
2. Enable **Routes API** as in §2.
3. Places API is **not required** for this app's Navigation tab — the
   Routes API's `Waypoint.address` field accepts free-text addresses
   directly and geocodes them internally, so typed addresses like "1600
   Amphitheatre Pkwy, Mountain View, CA" work as-is. Places API would only
   be useful if you wanted live autocomplete-while-typing, which this app
   doesn't implement.
4. Once you have a Routes API key (**APIs & Services** → **Credentials**
   → **Create Credentials** → **API key**, same flow as any other Google
   Cloud API key), paste it into google-tui's Settings tab → Navigation
   sub-tab.

Skip this section entirely if you don't need the Navigation tab — every
other API in this project is free.

## 7. Run the local auth flow to mint a token

google-tui reads Google credentials from `~/.hermes/google_token.json` —
a small JSON file with an access token, a `refresh_token`, and the scopes
you authorized (see `google_tui/gauth.py`). Use the client secret from
step 5 to run any standard Google OAuth "installed app" flow once (for
example, the `google-auth-oauthlib` `InstalledAppFlow.run_local_server()`
helper) requesting these scopes:

```
https://www.googleapis.com/auth/gmail.modify
https://www.googleapis.com/auth/calendar
https://www.googleapis.com/auth/drive
https://www.googleapis.com/auth/tasks
https://www.googleapis.com/auth/contacts.readonly   # once Contacts/People lands
```

Save the resulting credentials as `~/.hermes/google_token.json`. Once
that file exists, launch `google-tui` — the onboarding wizard won't
appear again for the Google side of setup.

## 8. AI provider setup (Ask pane)

Separate from the Google side — see `google_tui/setup_instructions.py`'s
`AI_PROVIDER_SETUP_STEPS`, or just open Settings in the app (`Ctrl+5`) and
pick a provider:

- **Hermes** (default): paste a Nous API key into Settings, or set
  `api_key:` in `~/.hermes/config.yaml`.
- **Claude Code**: install the `claude` CLI and run `claude` once
  interactively to log in.
- **opencode**: install the `opencode` CLI and configure it per
  [opencode.ai/docs](https://opencode.ai/docs/).
- **Gemini CLI**: install the `gemini` CLI and log in.

Whichever you pick, google-tui builds your Google context locally (recent
email/events) and hands it to that provider as part of the prompt — no
separate Google integration needed inside the AI CLI itself.

## 9. Browser tab search — Google Custom Search setup (optional)

The Browser tab's Search mode (bare text with no scheme in the address
bar) works out of the box with **DuckDuckGo** — no account, no API key,
nothing to configure. If you'd rather use **Google** search results (the
default provider — Settings → Search sub-tab), you need two things from a
*separate* Google product than the Workspace APIs above: the **Custom
Search JSON API** and a **Programmable Search Engine**. This is a
different console than the OAuth setup in §1–7, so don't reuse those
credentials here.

1. **Create (or reuse) a Programmable Search Engine.**
   1. Go to [programmablesearchengine.google.com](https://programmablesearchengine.google.com/)
      and sign in with the same Google account (any account works — this
      doesn't need to be the account whose Gmail/Calendar/Drive google-tui
      reads).
   2. **Add** → give it any name (e.g. "google-tui search").
   3. Under **What to search**, choose **Search the entire web** — a
      Programmable Search Engine defaults to a curated list of sites
      otherwise, which isn't what you want for a general-purpose Browser
      tab.
   4. Click **Create**, then open the new search engine's **Overview** —
      or **Setup** → **Basics** on older UI — and copy the **Search engine
      ID**. This is the value that goes in google-tui's Settings as
      `google_cse_id` (labeled "Search Engine ID (cx)" in the Search
      sub-tab — Google's API calls this parameter `cx`).

2. **Enable the Custom Search JSON API and get an API key.**
   1. Go back to [console.cloud.google.com](https://console.cloud.google.com/)
      — you can use the same project from §1, or a fresh one; this API
      isn't affected by the OAuth consent/test-user setup in §3–4.
   2. **APIs & Services** → **Enabled APIs & services** → **+ Enable APIs
      and Services** → search for and enable **Custom Search API**.
   3. **APIs & Services** → **Credentials** → **+ Create Credentials** →
      **API key**. This mints a plain API key (not an OAuth client — no
      consent screen, no test users, no expiry).
   4. Optional but recommended: click the new key → **Restrict key** →
      under **API restrictions**, limit it to **Custom Search API** only,
      so a leaked key can't be used against your other enabled APIs.
   5. Copy the key — this is `google_cse_api_key` in google-tui's Settings
      (labeled "Google Custom Search API key," entered as a password-style
      field).

3. **Enter both values in google-tui.** Settings tab → **Search** sub-tab
   → paste the API key and Search Engine ID into their fields → **Save
   search settings**. With `search_provider` left on the default
   **Google**, the Browser tab's Search mode now calls the real Custom
   Search JSON API; if the call ever fails (bad key, quota exceeded,
   network issue), it automatically falls back to DuckDuckGo for that
   search rather than showing an error.

**Free tier:** the Custom Search JSON API includes 100 free queries/day;
beyond that it's billed per 1,000 queries. Fine for a personal tool used
interactively; if you expect to blow past 100 searches/day, either enable
billing on this API specifically or just leave `search_provider` on
DuckDuckGo, which has no quota at all for this app's usage pattern.
