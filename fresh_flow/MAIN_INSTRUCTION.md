# Flow Image Generator — MCP Server para Claude Code

## Objetivo

Crear un **MCP server** que Claude Code pueda usar para generar imágenes usando Google Flow **sin consumir la cuota diaria del chat (~10/día)**.

El sistema actual usa un script Python con Playwright que llama directamente a la API `batchGenerateImages`.

## Archivos en este zip

```
flow-mcp/
├── MAIN_INSTRUCTION.md          ← este archivo
├── IMPLEMENTATION_GUIDE.md      ← guía técnica detallada
├── scripts/
│   └── generate.py              ← script funcional actual (referencia)
└── references/
    ├── auth-hang-diagnosis.md   ← troubleshooting de auth
    └── linux-arm64-xvfb-setup.md ← setup headless ARM64
```

## Lo que tiene que hacer Claude Code

1. **Leer `IMPLEMENTATION_GUIDE.md`** — arquitectura del MCP server
2. **Leer `scripts/generate.py`** — código funcional actual que hay que convertir
3. **Diseñar e implementar el MCP server** con herramienta `generate_image`
4. **Probar que funciona**

## Requisitos técnicos clave

- ⚠️ **NO usar CLI de gflow** — cuota ~10 imágenes/día
- ⚠️ **Usar `batchGenerateImages` directa desde browser** — cuota ilimitada
- ⚠️ **Sesión persistente:** `~/.local/share/gflow-cli/profile_cesaralarcon080405/`
- ⚠️ **Requiere Xvfb en `:99`**
- ⚠️ **Content filter silencioso** — prompts spicy devuelven None

## Setup previo

```bash
pgrep -a Xvfb || Xvfb :99 -screen 0 1280x720x24 &
gflow auth list
```
