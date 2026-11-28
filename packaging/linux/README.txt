Agent-Hire for Linux
====================

Install or update
-----------------
    ./install.sh

The installer asks for a folder (default ~/Agent-Hire). The app and everything it stores live
there: your profile, documents, the job index, chats, logs and caches. The app runs sandboxed to
that folder (with bubblewrap, if installed).

Running install.sh from a newer package updates an existing install. Your data and settings are
kept, and only the app is replaced.

Requirements: 64-bit Linux with glibc 2.39 or newer (Ubuntu 24.04, Debian 13, Fedora 40 or later),
curl, and an internet connection during installation. Recommended: poppler-utils (reads PDF
résumés) and bubblewrap (the sandbox).

AI keys
-------
If your shell already exports a provider key (OPENAI_API_KEY, GEMINI_API_KEY, OPENCODE_API_KEY,
ANTHROPIC_API_KEY, ... or the short forms like OPENAI_API), Agent-Hire uses it automatically. You
can also paste a key in Settings, or put it in the install folder's .env file.

Decision model: set AGENT_HIRE_DECISION_API_URL (plus _KEY and _MODEL) in the install folder's .env,
or enter it in the app under Settings → Job agent.

Browser extension
-----------------
In Chrome or Edge, open chrome://extensions, switch on Developer mode, choose "Load unpacked" and
pick the extension folder inside your install folder. Updates replace that folder in place, so
press the reload button on the extension card after updating.

Everyday use
------------
    <install folder>/agent-hire open       start (if needed) and open the app
    <install folder>/agent-hire stop       stop it
    <install folder>/agent-hire status     is it running?
    <install folder>/agent-hire uninstall  remove it
