# POE Live Alert

A personal, local-only browser interface for monitoring a Path of Exile 2 live trade search and optionally requesting travel to the listing's hideout.

## Install

Open PowerShell in this folder and run:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Run

Double-click `start.bat`, or run:

```powershell
.\.venv\Scripts\python.exe app.py
```

The app opens `http://127.0.0.1:8765` in your browser. It is deliberately bound only to your computer.

Enter:

1. **Cookie Header** — copy the complete `cookie` request-header value from an authenticated Path of Exile request. Never share this value.
2. **Search URL** — for example, `https://www.pathofexile.com/api/trade2/search/poe2/Runes%20of%20Aldur`.
3. **Search Payload** — the JSON request body for the custom search.

Use **Start Dry Run** first. It listens for and displays new matches without requesting travel. **Start & Travel to Hideout** requires confirmation and requests travel for the first match, then pauses automatic travel while you handle that trade. New matches are displayed as skipped while busy. Click **Ready for Next Alert** when the trade is finished to allow travel for the next match.

Press **Stop** in the page or `Ctrl+C` in PowerShell when finished.

When travel returns `false` or `{"success":false}` without an error, the app immediately retries once with the same token and `"continue":true` to confirm travel for an in-demand item. Errors such as "Item no longer available" are displayed without another retry; monitoring continues. Accepted travel pauses further travel until you click **Ready for Next Alert**.

## Credential handling

- The Cookie Header is sent only from the local page to the local Python process.
- It is held only in memory while the monitor is running.
- It is not saved to browser storage, files, or logs.
- The non-secret Search URL and Search Payload are remembered in browser local storage.
- The service listens on `127.0.0.1`, not the local network.

If a Cookie Header is ever exposed, log out of the Path of Exile website (use log out of all sessions if available) and sign in again before using the replacement.
