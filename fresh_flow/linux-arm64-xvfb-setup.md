# Headless Linux ARM64 & Xvfb Setup for gflow-cli

Running `gflow-cli` on headless Linux servers (especially ARM64 architecture, like AWS or Oracle Cloud instances) requires a specific configuration to bypass Playwright's system Chrome dependency and handle headless rendering.

## Diagnostic / Failure Symptoms

1. **TargetClosedError / Silent Exits (Exit Code 1):**
   - **Error:** Playwright fails immediately during `persistent_context_launch` with `TargetClosedError`.
   - **Cause:** The profile directory contains a `.gflow_browser_strategy` set to `chrome`. Playwright tries to launch system Chrome which is either not installed, not fully compatible with ARM64/headless, or lacks Decryption Keychains (which works fine on macOS but fails on Linux).

2. **UiSelectorDriftError / Black Screen on First Run:**
   - **Error:** `probe=mode_switch_trigger: no matching element found on the Flow editor.`
   - **Visual check (vision_analyze):** Screenshot is completely black.
   - **Cause:** The first cold-run of Google Flow on a fresh profile creates a new scratch project, which can take up to 60 seconds to fully render. Playwright times out waiting for the mode trigger selector.

---

## Step-by-Step Fixes & Setup

### 1. Force Bundled Chromium (Saves Decryption & ARM64 Pathing)
Remove the Chrome strategy marker from the profile so `gflow-cli` falls back to Playwright's bundled Chromium.

```bash
# Locate and remove the strategy file
rm -f ~/.local/share/gflow-cli/profile_<profile_name>/.gflow_browser_strategy

# Clear any stale SingletonLock or profile locks
rm -f ~/.local/share/gflow-cli/profile_<profile_name>/SingletonLock
```

### 2. Configure Virtual Framebuffer (Xvfb)
`gflow-cli` runs headed browser automation under the hood. Headless Linux servers must simulate a display.

```bash
# Verify if Xvfb is already running
ps aux | grep -i xvfb

# Start Xvfb on display :99 if not running
Xvfb :99 -screen 0 1280x720x24 &
```

### 3. Run Commands with the Display Prefix
Always prefix all `gflow` commands with the `DISPLAY` environment variable:

```bash
DISPLAY=:99 gflow image t2i "your prompt" --model nano2 --count 1
```

### 4. Cold-Run Drift Workaround
If the very first command fails with a selector drift or black screen, **simply retry it**. The second attempt will hit a pre-loaded/active project, making the generation extremely fast (under 10 seconds) and completely stable.
