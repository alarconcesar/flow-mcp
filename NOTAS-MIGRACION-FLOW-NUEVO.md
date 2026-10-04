# Diagnóstico: flow-mcp roto tras migración de Google Flow a flow.google.com

**Fecha:** 2026-09-26 (madrugada) · **Sesión:** auditoría VPS + re-autenticación

## Síntoma
- `flow-mcp generate` → `Failed to capture Bearer token — auth session may be expired`.
- `flow-mcp auth login` nunca detecta el login (se queda en "Waiting for sign-in").
- La CUENTA de Google SÍ está viva y logueada (verificado visualmente: avatar, PLUS, proyectos).

## Causa raíz (verificada empíricamente)
Google migró Flow de `labs.google/fx/tools/flow` → **`flow.google.com`**, y con eso cambió la API interna:

1. **Redirect**: `labs.google/fx/tools/flow` ahora redirige a `flow.google.com`.
2. **Auth interna nueva**: la app usa `POST https://flow.google.com/_/AiSandboxAngularFrontend/data/batchexecute?rpcids=...`
   con cookies + parámetro `at=AIQ-...` en el body (token de formato NUEVO, ya no `ya29...`).
   → El listener de `capture_bearer_token` (browser.py:77) busca `Authorization: Bearer ya29` en headers: ya no existe en el tráfico.
3. **Fetch cross-origin bloqueado**: `fetch('https://aisandbox-pa.googleapis.com/v1/flow/creditBalance')`
   desde `flow.google.com` → `TypeError: Failed to fetch` (CORS/ORB). El MCP depende de estos fetch
   inyectados (js_templates.py) hacia `aisandbox-pa` y `labs.google/fx/api/trpc/*`.
4. **Sesión API vieja muerta**: `labs.google/fx/api/auth/session` → `200 {}` (vacío).
   `flow.google.com/api/auth/session` → 404.
5. Sin requests a `aisandbox-pa` durante carga de home/proyecto → la app nueva ya no los usa en esas vistas.

## Qué sigue funcionando
- Sesión del perfil `cesaralarcon080405` ✓ (cookies Google OK — Gmail, Flow, myaccount entran directo).
- El perfil, los binarios, el pool de browser, Xvfb ✓.

## Palancas para el fix (proyecto)
- [ ] Reverse del nuevo flujo: disparar una generación real en la UI y capturar requests
      (¿batchexecute? ¿aisandbox-pa con otros headers? ¿service worker?) — usar Playwright con
      listener de requests + post_data (script base: `/tmp/flow_token_test.py`, `flow_traffic_check.py`).
- [ ] Ver si `labs.google` sigue sirviendo la app VIEJA sin redirect (entonces quizá basta fijar la URL).
- [ ] Alternativa: obtener un `ya29` por OAuth directo (device flow con client_id de Google Labs)
      en lugar de scrapear el navegador.
- [ ] Los scripts de debug de la sesión quedaron copiados en `debug-2026-09/`.
- [ ] ¿Upstream nuevo? El repo no tiene commits post-22-jul; rama `feature/multi-account` (22-23-jul)
      tampoco contempla la migración.

## Notas
- El proyecto "sept 26 - 02:04" (flow.google.com/project/a9591705-...) se creó al probar (borrable).
- Re-login NO era la solución (la sesión nunca fue el problema; el código sí).
