# Guía de Implementación — Flow MCP Server

## Arquitectura

```
Claude Code → MCP Server (flow_image_server.py) → generate_image() → Playwright → Google Flow API
```

Protocolo: **stdio-based MCP** con SDK `mcp` de Python.

## Herramienta

### `generate_image`

```json
{
  "name": "generate_image",
  "description": "Generate an image via Google Flow API",
  "inputSchema": {
    "type": "object",
    "properties": {
      "prompt": {"type": "string"},
      "model": {"type": "string", "enum": ["nano-pro", "nano2", "narwhal", "gem_pix_2"], "default": "nano-pro"},
      "count": {"type": "integer", "minimum": 1, "maximum": 4, "default": 1},
      "aspect": {"type": "string", "enum": ["9:16", "16:9", "1:1", "4:3", "3:4"], "default": "9:16"}
    },
    "required": ["prompt"]
  }
}
```

**Returns:** `{"success": true, "files": ["/tmp/flow-gen-xxx.webp"]}`

## Core logic (de `scripts/generate.py`)

1. Launch Playwright con `user_data_dir` persistente
2. Navegar a `labs.google/fx` → capturar Bearer token de headers
3. Mintear reCAPTCHA: `TokenMinter(page).mint("IMAGE_GENERATION")`
4. Crear proyecto vía tRPC: `project.createProject`
5. Llamar `batchGenerateImages` con fetch desde el browser
6. Parsear `fifeUrl`, descargar, guardar

### Manejo de errores
- **401:** retry automático (refresca OAuth cookies)
- **Content filter (None):** raise error claro
- **reCAPTCHA falla:** retry

## Detalles

### Playwright
- `headless=False`, `user_data_dir=~/.local/share/gflow-cli/profile_cesaralarcon080405/`
- Args: `--no-sandbox --password-store=basic --disable-gpu --disable-dev-shm-usage --disable-blink-features=AutomationControlled`

### APIs (desde browser con `add_script_tag`)
1. `POST https://labs.google/fx/api/trpc/project.createProject`
2. `POST https://aisandbox-pa.googleapis.com/v1/projects/{pid}/flowMedia:batchGenerateImages`

### Bearer token
Se captura con `page.on("request", ...)` → header `authorization: Bearer ya29...`

### Mappings
```python
MODEL_MAP = {"nano2":"NARWHAL","nano-pro":"GEM_PIX_2","narwhal":"NARWHAL","gem_pix_2":"GEM_PIX_2"}
ASPECT_MAP = {"9:16":"IMAGE_ASPECT_RATIO_PORTRAIT","16:9":"IMAGE_ASPECT_RATIO_LANDSCAPE",
              "1:1":"IMAGE_ASPECT_RATIO_SQUARE","4:3":"IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE",
              "3:4":"IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR"}
```

## Dependencias

```txt
mcp>=1.0.0
playwright>=1.40.0
gflow-cli>=0.1.0
```

## Test

```bash
DISPLAY=:99 timeout 90 python3 scripts/generate.py \
  "una oficina moderna con personas trabajando en laptops" \
  --model nano-pro --count 1 --aspect 16:9
```
