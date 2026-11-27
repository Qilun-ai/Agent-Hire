Agent-Hire for Windows
======================

Install or update
-----------------
Double-click "Install Agent-Hire.cmd".

The installer asks for a folder (default: your user folder\Agent-Hire). The app and everything it
stores live there: your profile, documents, the job index, chats, logs and caches. The app's
profile, AppData and temp folders are redirected into that folder as well.

Running the installer from a newer package updates an existing install. Your data and settings
are kept, and only the app is replaced.

Requirements: 64-bit Windows 10 or 11, and an internet connection during installation.
Recommended: Poppler for reading PDF resumes (winget install oschwartz10612.Poppler).

AI keys
-------
If a provider key is set as a Windows environment variable (OPENAI_API_KEY, GEMINI_API_KEY,
OPENCODE_API_KEY, ANTHROPIC_API_KEY, ... or the short forms like OPENAI_API), Agent-Hire uses it
automatically. You can also paste a key in Settings, or put it in the install folder's .env file.

Decision model: set AGENT_HIRE_DECISION_API_URL (plus _KEY and _MODEL) in the install folder's .env,
or enter it in the app under Settings -> Job agent.

Browser extension
-----------------
In Chrome or Edge, open chrome://extensions (edge://extensions), switch on Developer mode, choose
"Load unpacked" and pick the extension folder inside your install folder. Updates replace that
folder in place, so press the reload button on the extension card after updating.

Everyday use
------------
Start menu -> Agent-Hire opens the app. From a terminal in the install folder:
    agent-hire.cmd open        start (if needed) and open the app
    agent-hire.cmd stop        stop it
    agent-hire.cmd status      is it running?
    agent-hire.cmd uninstall   remove it
