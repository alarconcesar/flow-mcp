# Google Flow MCP 🎨

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org)
[![MCP](https://img.shields.io/badge/MCP-2024--11--05-purple)](https://modelcontextprotocol.io)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**MCP server for generating images via Google Flow — no daily quota limits.**

Claude Code (or any MCP client) can generate images using Google Flow's
`batchGenerateImages` API *directly* through a Playwright browser context
with your saved authentication, bypassing the Flow Agent chat quota (~10
images/day).

## Features

- **Text-to-Image** — generate images from text prompts
- **Image-to-Image** — use a reference image (pass `reference_image`)
- **Text-to-Video** — generate videos from text prompts (pass `generate_video` tool)
- **Image-to-Video** — animate a local image into a 4-8s video clip
- **No quota limits** — calls the API directly, not through the chat
- **Multi-account fallback** — rotates across Google accounts when one runs out of credits
- **Persistent browser pool** — reuses Chrome across generations (faster)
- **Auto-retry** — refreshes auth token if it expires
- **Progress reporting** — shows generation progress in Claude Code
- **No external CLI dependencies** — includes its own auth/login
- **Cross-platform** — Windows, macOS, Linux (incl. headless)

## Requirements

| Dependency | Notes |
|------------|-------|
| **Python 3.11+** (or [uv](https://docs.astral.sh/uv/)) | |
| **Google Chrome** or Playwright's Chromium | For authentication & generation |
| **Xvfb** (Linux headless only) | For `auth login` only (MCP server runs headless) |

## Installation

### Prerequisites

- **Python 3.11+**
- **Google Chrome** (for authentication)
- **Playwright browsers** (for generation)

### Install from PyPI (recommended)

```bash
pip install flow-mcp

# Or with uv:
# uv pip install flow-mcp

# Install Playwright browsers
playwright install chromium

# Authenticate with Google Flow
flow-mcp auth login
```

### Or install from GitHub

```bash
git clone https://github.com/alarconcesar/flow-mcp.git
cd flow-mcp
uv pip install -e .
playwright install chromium
flow-mcp auth login
```

## Usage with Claude Code

Add to your `.claude/settings.json`:

```json
{
  "mcpServers": {
    "flow-image-server": {
      "command": "uv",
      "args": ["run", "flow-mcp"]
    }
  }
}
```

Restart Claude Code. The `generate_image` and `generate_video` tools will be available.

### Image Parameters (`generate_image`)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `prompt` | string | **required** | Text description of the image |
| `model` | enum | `nano-pro` | `nano2`, `nano-pro`, `narwhal`, `gem_pix_2` |
| `count` | integer | `1` | Number of images (1–4) |
| `aspect` | enum | `9:16` | `9:16`, `16:9`, `1:1`, `4:3`, `3:4` |
| `reference_image` | string | optional | Path to a local image for I2I |

### Video Parameters (`generate_video`)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `prompt` | string | **required** | Text description of the video |
| `model` | enum | `veo-fast` | `veo-fast` (cheapest), `veo-2-fast` (confirmed working), `veo` (standard), `veo-hq` |
| `aspect` | enum | `9:16` | `9:16`, `16:9`, `1:1` |
| `duration` | integer | `4` | Clip length in seconds: `4` (cheapest), `6`, `8` |
| `reference_image` | string | optional | Path to a local image to animate (I2V) |

**Cost warning**: Video is significantly more expensive than image generation. A single 4s clip can cost ~20-50 credits. Prefer `veo-fast` + 4s duration to minimize consumption.

**Download Note**: As of Jul 2026, Flow's public tRPC API does not expose direct download URLs for video (only images work). The `generate_video` tool automatically returns the **project URL** where the video is rendered. Open this URL in your desktop browser to watch and click "Download" manually.

### Examples

**Text-to-Image:**
```
Generate an image of a cyberpunk city at night, neon lights, 16:9
```

**Image-to-Image:**
```
Take this photo and make it cyberpunk style,
reference_image: /Users/me/photo.jpg
```

## CLI commands

```bash
flow-mcp                           # Start MCP server (stdio mode)
flow-mcp auth login [name]         # Authenticate (optionally as <name>)
flow-mcp auth login --browser internal [name]   # Login via Playwright's Chromium
flow-mcp auth list                 # List saved profiles
flow-mcp auth accounts             # List accounts with priority order
flow-mcp auth switch [name]        # Switch active account (next, or by name)
flow-mcp auth remove <name>        # Remove a specific account (asks confirmation)
flow-mcp auth logout [name]        # Remove active account (or <name>)
flow-mcp credits                   # Check remaining credits
flow-mcp help                      # Show help
```

## Multi-account support

flow-mcp can rotate across multiple Google Flow accounts automatically.
When the active account runs out of credits (Google returns HTTP 403 with
"credit"/"quota" in the body), the generator switches to the next
configured account and retries the same request. If every account in the
list is exhausted, generation fails with a clear message naming all the
accounts that were tried.

### Setup

```bash
# 1. Authenticate each account under a distinct profile name
flow-mcp auth login personal
flow-mcp auth login work
flow-mcp auth login backup

# 2. Define priority order via env var
export GFLOW_ACCOUNTS=personal,work,backup
```

Without `GFLOW_ACCOUNTS`, flow-mcp auto-detects every authenticated
profile and uses them in filesystem order. Profiles that don't exist on
disk (e.g. typos in the env var) are filtered out and logged as
warnings.

### Manual control

```bash
flow-mcp auth accounts            # show all configured accounts and priority
flow-mcp auth switch              # jump to the next account in the list
flow-mcp auth switch work         # jump to a specific account by name
flow-mcp auth remove backup       # delete an account (asks confirmation)
```

The active account name is persisted to `.gflow_active_account` inside
`GFLOW_CLI_HOME` so the choice survives restarts.

### Important

- Each profile stores its own Chrome cookies under
  `~/.local/share/gflow-cli/profile_<name>/`. Logging in to multiple
  accounts on the same machine is fully supported — they never share
  state.
- The browser pool is closed and re-opened between accounts so each
  account uses its own profile directory.
- Multi-account is opt-in. If you only ever use one account, nothing
  changes — just don't set `GFLOW_ACCOUNTS`.

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GFLOW_ACCOUNTS` | auto-detect | Comma-separated profile names in priority order (e.g. `personal,work,backup`). Missing names are filtered out with a warning. |
| `GFLOW_PROFILE` | auto-detected | Profile name (single-account mode) |
| `GFLOW_CLI_HOME` | *platform default* | gflow-cli data directory |
| `GFLOW_OUTPUT_DIR` | temp directory | Where to save generated images |

## Troubleshooting

### Auth expired
```
flow-mcp auth login
```

### Content filter
Google Flow silently blocks certain prompts (returns `None`).
Try rephrasing — avoid violence, NSFW, or trademarked content.

### Linux headless
```bash
# Only needed for authentication (the MCP server itself runs headless)
Xvfb :99 -screen 0 1280x720x24 &
DISPLAY=:99 flow-mcp auth login --browser internal
```

The MCP server (`flow-mcp`) now runs completely headless — no Xvfb needed for generation.

### Profile not found
```bash
flow-mcp auth list         # list profiles
flow-mcp auth login        # create a new profile
```

## Project structure

```
flow-mcp/
├── src/
│   └── flow_mcp/
│       ├── __init__.py        # Package metadata & logging config
│       ├── __main__.py        # CLI entry point (auth, server)
│       ├── server.py          # FastMCP tool definition
│       ├── generator.py       # Core generation logic
│       ├── browser.py         # Playwright context & token capture
│       ├── browser_pool.py    # Persistent browser context pool
│       ├── account_manager.py # Multi-account rotation + fallback
│       ├── auth.py            # Login, profile list, remove, switch
│       ├── profile.py         # Profile resolution
│       ├── recaptcha.py       # reCAPTCHA token minting
│       ├── chrome_helpers.py  # Chrome detection
│       └── constants.py       # Shared constants
├── pyproject.toml
├── LICENSE (MIT)
└── README.md
```

## License

MIT — see [LICENSE](LICENSE).
