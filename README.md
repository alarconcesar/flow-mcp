# Flow MCP 🎨

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
- **No quota limits** — calls the API directly, not through the chat
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
| **Xvfb** (Linux headless only) | `:99` display |

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

Restart Claude Code. The `generate_image` tool will be available.

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `prompt` | string | **required** | Text description of the image |
| `model` | enum | `nano-pro` | `nano2`, `nano-pro`, `narwhal`, `gem_pix_2` |
| `count` | integer | `1` | Number of images (1–4) |
| `aspect` | enum | `9:16` | `9:16`, `16:9`, `1:1`, `4:3`, `3:4` |
| `reference_image` | string | optional | Path to a local image for I2I |

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
flow-mcp                    # Start MCP server (stdio mode)
flow-mcp auth login         # Authenticate with Google Flow
flow-mcp auth list          # List saved profiles
flow-mcp help               # Show help
```

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GFLOW_PROFILE` | auto-detected | Profile name |
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
Xvfb :99 -screen 0 1280x720x24 &   # or let flow-mcp auto-start it
flow-mcp auth login --browser internal
```

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
│       ├── __init__.py      # Package metadata & logging config
│       ├── __main__.py      # CLI entry point (auth, server)
│       ├── server.py        # FastMCP tool definition
│       ├── generator.py     # Core generation logic
│       ├── browser.py       # Playwright context & token capture
│       ├── browser_pool.py  # Persistent browser context pool
│       ├── auth.py          # Login, profile list commands
│       ├── profile.py       # Profile resolution
│       ├── recaptcha.py     # reCAPTCHA token minting
│       ├── chrome_helpers.py # Chrome detection
│       └── constants.py     # Shared constants
├── pyproject.toml
├── LICENSE (MIT)
└── README.md
```

## License

MIT — see [LICENSE](LICENSE).
