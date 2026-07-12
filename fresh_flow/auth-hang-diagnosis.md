# Auth Hang Diagnosis (gflow-cli)

## The Silent Hang

**Symptom:** `gflow image t2i` creates the Flow project, logs `ui_driver.bound`, and then sits there forever (until shell timeout, exit 124). No error, no progress, no output.

**Root cause:** The Flow API authentication token expired. The Google cookies (`Session: present` in `gflow auth list`) are still valid, but the per-API token that Flow's backend uses is stale.

## Why `ui_automation` Hides This

The default `ui_automation` transport drives the Flow editor UI via Playwright (like a user clicking buttons). When the API token is stale, the UI automation never gets a response from the generation endpoint — it just waits on a DOM element that never updates. No error is surfaced because the transport doesn't inspect HTTP responses.

## Diagnostic: `evaluate_fetch`

The `evaluate_fetch` transport (enabled via `GFLOW_CLI_EXPERIMENTAL_TRANSPORTS=1`) does a real API call in the teardown phase and surfaces HTTP 401 immediately:

```bash
DISPLAY=:99 GFLOW_CLI_EXPERIMENTAL_TRANSPORTS=1 GFLOW_CLI_TRANSPORT=evaluate_fetch \
  gflow image t2i "test" --model nano2 --count 1 --project <existing_project_id>
```

Expected success output: `Image generated and saved to ...`
Expected failure output: `AuthExpiredError: HTTP 401 persisted after refresh — session expired`

## Auth Refresh Pitfall

`gflow auth login --browser internal` can print:
```
Session saved. Profile dir: ...
Set <name> as default profile.
```

...yet the Flow API token is NOT refreshed. This happens because the internal login refreshes the Google session cookie, but Flow's backend still considers the API token tied to the old session.

**How to verify a REAL fix:**
1. Run `evaluate_fetch` diagnostic above
2. If it passes (no 401), you're good
3. If it still fails 401, proceed to **interactive VNC login**

## Full Resolution Steps (Interactive VNC Login)

Discovered and tested 2026-07-05:

1. **Kill stale Chrome processes** that may hold locks:
   ```bash
   pkill -f google-chrome 2>/dev/null
   pkill -f playwright 2>/dev/null
   ```

2. **Ensure Xvfb is running**:
   ```bash
   pgrep -a Xvfb || Xvfb :99 -screen 0 1280x720x24 &
   ```

3. **Launch fluxbox** (window manager so the desktop isn't blank):
   ```bash
   fluxbox &
   ```

4. **Start x11vnc**:
   ```bash
   x11vnc -display :99 -passwd <choose_a_password> -forever -quiet &
   ```

5. **Start noVNC + websockify**:
   ```bash
   python3 -m http.server 6081 --directory /usr/share/novnc &
   websockify 6082 localhost:5900 &
   ```

6. **Tell the user to open**: `http://<tailscale-ip>:6081/vnc.html` in their browser
   - Get tailscale IP: `tailscale ip`
   - In noVNC page, connect to `127.0.0.1:6082` with the password from step 4

7. **Launch Chrome** on the remote desktop:
   ```bash
   DISPLAY=:99 google-chrome --no-sandbox https://labs.google/fx &
   ```

8. **User signs in** normally with their Google credentials on the Flow page

9. **Verify**:
   ```bash
   gflow auth list
   verify with evaluate_fetch transport
   ```

10. **Cleanup** (kill background processes afterwards)
