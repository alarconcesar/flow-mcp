"""Transporte batchexecute de Google Flow — la migración de sep-2026.

El mundo viejo era un POST REST a ``aisandbox-pa.googleapis.com`` con un
``Bearer ya29…`` que se soplaba de la página. Ese token ya no existe: la app
reescrita en ``flow.google.com`` firma cada llamada con la cookie de sesión más
un token ``at`` por página, contra un único endpoint batchexecute, y cada
generación lleva un reCAPTCHA **de un solo uso** (uno repetido vuelve como
``PUBLIC_ERROR_UNUSUAL_ACTIVITY``).

Nada de eso se puede replicar desde fuera del navegador, así que este módulo
nunca hace red por su cuenta: arma envelopes ``f.req``, los ejecuta dentro de
la pestaña de Flow (``page.evaluate``) y lee las respuestas.

Formato de cable (batchexecute estándar de Google)::

    f.req = [[[rpcid, "<payload interno como string JSON>", null, "generic"]]]

y la respuesta es el centinela ``)]}'`` seguido de chunks con prefijo de
longitud ``["wrb.fr", rpcid, "<payload como string JSON>", …]``.

Protocolo derivado de la implementación pública de flowkit
(https://github.com/crisng95/flowkit, MIT); aviso de licencia completo en
``LICENSES/flowkit-MIT.txt``.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import structlog
from playwright.async_api import Page

log = structlog.get_logger("flow-mcp")

# ── Endpoints y RPCs ──────────────────────────────────────────────────────
FLOW_HOME = "https://flow.google.com/"
BATCH_PATH = "/_/AiSandboxAngularFrontend/data/batchexecute"
MEDIA_HOST = "flow-content.google"

RPC_GEN_IMAGE = "ogiZ0b"
RPC_OPERATION = "jwpduf"
RPC_CREATE_PROJECT = "jHPbke"
RPC_PROJECT_MEDIA = "Zzl0ze"
RPC_MEDIA = "as29s"
RPC_UPLOAD_IMAGE = "maseQ"
RPC_UPSCALE_IMAGE = "SPrCad"

CAPTCHA_IMAGE = "IMAGE_GENERATION"
CAPTCHA_VIDEO = "VIDEO_GENERATION"

#: El reCAPTCHA se mintea en la página, momentos antes de que salga el
#: request, así que el envelope viaja con este marcador y se sustituye acá.
CAPTCHA_SLOT = "__CAPTCHA__"

# ── Modelos ───────────────────────────────────────────────────────────────
IMAGE_MODELS = {"GEM_PIX_2", "NARWHAL", "HARBOR_SEAL"}
IMAGE_MODEL = "GEM_PIX_2"
IMAGE_MODEL_BY_NICKNAME = {
    # Nombres comerciales de la app nueva.
    "NANO_BANANA_PRO": "GEM_PIX_2",
    "NANO_BANANA_2": "NARWHAL",
    "NANO_BANANA_2_LITE": "HARBOR_SEAL",
    "NANO_BANANA_LITE": "HARBOR_SEAL",
    # Alias heredados de flow-mcp v0.3 (compatibilidad de CLI/MCP).
    "NANO2": "NARWHAL",
    "NANO-PRO": "GEM_PIX_2",
    "NARWHAL": "NARWHAL",
    "GEM_PIX_2": "GEM_PIX_2",
}

# ── Aspect ratios ─────────────────────────────────────────────────────────
#: Codificados por la app nueva (medidos generando uno de cada uno).
ASPECT_SQUARE = 1           # 1024x1024
ASPECT_PORTRAIT = 2         # 768x1376  (9:16)
ASPECT_LANDSCAPE = 3        # 1376x768  (16:9)
ASPECT_PORTRAIT_4_3 = 4     # 896x1200  (3:4)
ASPECT_LANDSCAPE_4_3 = 5    # 1200x896  (4:3)

ASPECT_BY_NAME = {
    "IMAGE_ASPECT_RATIO_SQUARE": ASPECT_SQUARE,
    "IMAGE_ASPECT_RATIO_PORTRAIT": ASPECT_PORTRAIT,
    "IMAGE_ASPECT_RATIO_LANDSCAPE": ASPECT_LANDSCAPE,
    "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR": ASPECT_PORTRAIT_4_3,
    "IMAGE_ASPECT_RATIO_PORTRAIT_FOUR_THREE": ASPECT_PORTRAIT_4_3,
    "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE": ASPECT_LANDSCAPE_4_3,
    "1:1": ASPECT_SQUARE,
    "9:16": ASPECT_PORTRAIT,
    "16:9": ASPECT_LANDSCAPE,
    "3:4": ASPECT_PORTRAIT_4_3,
    "4:3": ASPECT_LANDSCAPE_4_3,
}

# ── Otros ─────────────────────────────────────────────────────────────────
STATUS_DONE = "CAE"
OUTCOME_COMPLAINT = 4
SURFACE_ID = 22
FULL_FRAME_CROP = [None, None, 1, 1]
REF_TYPE_IMAGE = 1
BASE_TYPE_IMAGE = 2
IMAGE_UPSCALE_RESOLUTIONS = {"2K": 1, "4K": 2}

#: La app rechaza GEM_PIX_2 si ``source-path`` no va en la URL.
DEFAULT_MAX_CHARS = 4_000_000

#: Las generaciones se hacen desde la página del proyecto: la portada no carga
#: reCAPTCHA y el ``source-path`` del RPC tiene que ser el del proyecto.
PROJECT_URL = "https://flow.google.com/project/{pid}"


class FlowBatchError(RuntimeError):
    """El transporte batchexecute no pudo completar la operación."""


class NoAtTokenError(FlowBatchError):
    """La página de Flow no expuso ``WIZ_global_data.SNlM0e`` (sesión fría)."""


class RpcError(FlowBatchError):
    """Un envelope volvió con slot de error en vez de datos."""

    def __init__(self, rpcid: str, detail: Any):
        super().__init__(f"{rpcid} failed: {detail!r}")
        self.rpcid = rpcid
        self.detail = detail


@dataclass(slots=True)
class RpcResult:
    rpcid: str
    data: Any = None
    error: Any = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(slots=True)
class GeneratedImage:
    media_id: str
    url: str


@dataclass(slots=True)
class Operation:
    operation_id: Any
    project_id: Any = None
    status: Any = None
    error: Optional[str] = None

    @property
    def done(self) -> bool:
        return self.status == STATUS_DONE

    @property
    def complained(self) -> bool:
        return self.error is not None


@dataclass(slots=True)
class MediaUrls:
    media_id: str
    video: Optional[str] = None
    image: Optional[str] = None


# ══════════════════════════════════════════════════════════════════════════
#  Codec del envelope
# ══════════════════════════════════════════════════════════════════════════


def build_envelope(rpcid: str, inner: Any) -> str:
    """Envuelve un payload interno como el ``f.req`` que espera batchexecute."""
    return json.dumps(
        [[[rpcid, json.dumps(inner, separators=(",", ":"), ensure_ascii=False), None, "generic"]]],
        separators=(",", ":"),
        ensure_ascii=False,
    )


def parse_envelope(text: str) -> list[RpcResult]:
    """Quita el centinela ``)]}'`` y decodifica los chunks con prefijo de longitud.

    Los largos del prefijo cuentan caracteres, pero un payload con escapes
    puede discrepar por un byte o dos: se decodifica escaneando en vez de
    confiar en el prefijo.
    """
    if not text:
        return []
    body = text.split("\n", 1)[1] if text.startswith(")]}'") else text
    decoder = json.JSONDecoder()
    results: list[RpcResult] = []
    index = 0
    while index < len(body):
        start = body.find("[", index)
        if start == -1:
            break
        try:
            chunk, consumed = decoder.raw_decode(body[start:])
        except json.JSONDecodeError:
            # Un corte a mitad de token no debe costarnos los envelopes siguientes.
            index = start + 1
            continue
        index = start + consumed
        for entry in chunk if isinstance(chunk, list) else []:
            if not isinstance(entry, list) or not entry or entry[0] != "wrb.fr":
                continue
            rpcid = entry[1] if len(entry) > 1 else "?"
            payload = entry[2] if len(entry) > 2 else None
            if payload is None:
                # El índice 5 es el slot de error: códigos tipo [5], no texto.
                results.append(RpcResult(rpcid, None, entry[5] if len(entry) > 5 else True))
                continue
            results.append(
                RpcResult(rpcid, json.loads(payload) if isinstance(payload, str) else payload)
            )
    return results


def first_payload(text: str, rpcid: str) -> Any:
    """Payload del primer envelope que matchea, o levanta lo que salió mal."""
    results = parse_envelope(text)
    for result in results:
        if result.rpcid != rpcid:
            continue
        if not result.ok:
            raise RpcError(rpcid, result.error)
        return result.data
    raise FlowBatchError(f"no {rpcid} envelope in response ({len(results)} others)")


# ══════════════════════════════════════════════════════════════════════════
#  Builders
# ══════════════════════════════════════════════════════════════════════════


def _client_uuid() -> str:
    """Ids de request del cliente; la UI los manda en mayúsculas."""
    return str(uuid.uuid4()).upper()


def _context(project_id: Optional[str]) -> list:
    """Envelope superficie/proyecto/captcha que repite cada llamada de generación."""
    return [None, SURFACE_ID, None, None, None, project_id, None, None, None, None,
            [CAPTCHA_SLOT, 1]]


def _image_input(media_id: str, input_type: int) -> list:
    return [media_id, None, None, None, input_type]


def _reference(media_id: str) -> list:
    return _image_input(media_id, REF_TYPE_IMAGE)


def _base_image(media_id: str) -> list:
    return _image_input(media_id, BASE_TYPE_IMAGE)


def resolve_image_model(key: Optional[str]) -> str:
    """Acepta alias comerciales, alias viejos de flow-mcp o un id exacto."""
    if key is None:
        return IMAGE_MODEL
    token = str(key).strip()
    if not token:
        return IMAGE_MODEL
    upper = token.upper()
    if upper in IMAGE_MODEL_BY_NICKNAME:
        return IMAGE_MODEL_BY_NICKNAME[upper]
    if upper in IMAGE_MODELS:
        return upper
    raise ValueError(
        f"unknown image model {key!r} — use one of "
        f"{sorted(IMAGE_MODEL_BY_NICKNAME)} or an exact wire id"
    )


def resolve_aspect(aspect: Any) -> int:
    if isinstance(aspect, bool):
        raise ValueError("aspect must be a name or an integer, not a bool")
    if isinstance(aspect, int):
        if 1 <= aspect <= 5:
            return aspect
        raise ValueError("numeric aspect must be 1-5")
    key = str(aspect).strip()
    if key in ASPECT_BY_NAME:
        return ASPECT_BY_NAME[key]
    raise ValueError(
        f"unknown aspect {aspect!r} — use one of {sorted(ASPECT_BY_NAME)} or 1-5"
    )


def image_request(
    prompt: str,
    project_id: str,
    count: int = 1,
    aspect: Any = ASPECT_SQUARE,
    seed: Optional[int] = None,
    prompts: Optional[list[str]] = None,
    model: str = IMAGE_MODEL,
    ref_media_ids: Optional[list[str]] = None,
    base_media_id: Optional[str] = None,
    seeds: Optional[list[int]] = None,
) -> str:
    """Un item de request por variante: no hay campo "cuántas", Flow devuelve
    una imagen por item de la lista, así que ``count`` replica el item con
    semillas nuevas. ``ref_media_ids`` condiciona el resultado con imágenes que
    ya están en el proyecto (es lo que mantiene a un personaje siendo el mismo).
    """
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 4:
        raise ValueError("image count must be an integer from 1 to 4")
    ratio = resolve_aspect(aspect)
    resolved_model = resolve_image_model(model)
    base = seed if seed is not None else random.randint(1, 10**9)
    items = []
    for index in range(count):
        text = prompts[index] if prompts and index < len(prompts) else prompt
        item_seed = (
            seeds[index]
            if seeds and index < len(seeds) and isinstance(seeds[index], int)
            else base + index * 9973
        )
        image_inputs: list = []
        if base_media_id:
            image_inputs.append(_base_image(base_media_id))
        image_inputs.extend(
            _reference(mid) for mid in (ref_media_ids or []) if mid != base_media_id
        )
        items.append([None, None, image_inputs or None, item_seed, ratio,
                      resolved_model, None, _context(project_id), [[[text]]],
                      None, None, None, _client_uuid(), _client_uuid()])
    return build_envelope(RPC_GEN_IMAGE, [None, items, 1, _context(project_id),
                                          [_client_uuid()]])


def image_upscale_request(media_id: str, resolution: str = "2K") -> str:
    """Request de FlowService.UpsampleImage (RPC SPrCad)."""
    key = str(resolution).strip().upper()
    if key.startswith("UPSAMPLE_IMAGE_RESOLUTION_"):
        key = key.removeprefix("UPSAMPLE_IMAGE_RESOLUTION_")
    if key not in IMAGE_UPSCALE_RESOLUTIONS:
        raise ValueError("image upscale resolution must be 2K or 4K")
    return build_envelope(RPC_UPSCALE_IMAGE, [media_id, IMAGE_UPSCALE_RESOLUTIONS[key],
                                              _context(None)])


def upload_request(image_b64: str, project_id: str, mime_type: str = "image/jpeg",
                   file_name: str = "upload.jpg") -> str:
    """Sube una imagen local al proyecto para usarla como referencia.

    Los bytes viajan en el propio RPC como base64 plano (sin prefijo ``data:``
    ni endpoint aparte) y la llamada lleva captcha, igual que una generación.
    """
    return build_envelope(RPC_UPLOAD_IMAGE, [
        _context(project_id), image_b64, mime_type, 1, None, None, None, None,
        file_name, None, _client_uuid(), _client_uuid(),
    ])


def create_project_request(title: str) -> str:
    """Crea un proyecto de Flow (capturado de la UI de flow.google.com)."""
    clean = " ".join(str(title or "flow-mcp project").split())[:160] or "flow-mcp project"
    return build_envelope(RPC_CREATE_PROJECT, ["projects/*", [None, [clean]], [None, SURFACE_ID]])


def operation_request(operation_id: str) -> str:
    return build_envelope(RPC_OPERATION, [None, None, [[operation_id]]])


def project_media_request(project_id: str) -> str:
    return build_envelope(RPC_PROJECT_MEDIA, [f"projects/{project_id}", None, None, None, [1]])


def media_request(media_id: str) -> str:
    return build_envelope(RPC_MEDIA, [media_id])


# ══════════════════════════════════════════════════════════════════════════
#  Lectores de respuesta
# ══════════════════════════════════════════════════════════════════════════


def _walk_strings(node: Any):
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for item in node:
            yield from _walk_strings(item)


def _walk_lists(node: Any):
    if isinstance(node, list):
        yield node
        for item in node:
            yield from _walk_lists(item)


def read_images(payload: Any) -> list[GeneratedImage]:
    """Las URLs firmadas del CDN vuelven inline en la llamada de imagen.

    El media id se lee del path de la URL (no de un índice fijo): la URL es lo
    que realmente necesitamos y aparearlos en origen evita que un reordenamiento
    de la respuesta cruce ids con fotos.
    """
    images: list[GeneratedImage] = []
    seen: set[str] = set()
    for text in _walk_strings(payload):
        if MEDIA_HOST + "/image/" not in text:
            continue
        media_id = text.split("/image/", 1)[1].split("?", 1)[0]
        if media_id in seen:
            continue
        seen.add(media_id)
        images.append(GeneratedImage(media_id=media_id, url=text))
    return images


def read_uploaded_media_id(payload: Any) -> str:
    """``[[mediaId, projectId, operationId, "CAE", …]]`` — el handle de la referencia."""
    record = payload[0] if isinstance(payload, list) and payload else None
    media_id = record[0] if isinstance(record, list) and record else None
    if not isinstance(media_id, str) or not media_id:
        raise FlowBatchError("upload response carried no media id")
    return media_id


def read_upscaled_image(payload: Any) -> str:
    """Devuelve el cuerpo base64 de FlowService.UpsampleImage."""
    encoded = payload[1] if isinstance(payload, list) and len(payload) > 1 else None
    if not isinstance(encoded, str) or len(encoded) < 100:
        raise FlowBatchError("image upscale response carried no encoded image")
    return encoded


def read_created_project(payload: Any) -> tuple[str, Optional[str]]:
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], str):
        raise FlowBatchError("project create response did not contain a project id")
    title = payload[1][0] if len(payload) > 1 and isinstance(payload[1], list) and payload[1] else None
    return payload[0], title if isinstance(title, str) else None


def read_operation_error(record: list) -> Optional[str]:
    detail = record[5] if len(record) > 5 else None
    if not isinstance(detail, list) or len(detail) <= 8:
        return None
    block = detail[8]
    if not isinstance(block, list) or not block or block[0] != OUTCOME_COMPLAINT:
        return None
    for text in _walk_strings(block):
        return text
    return "operation failed without a message"


def read_operation(payload: Any) -> Operation:
    """``[null, 50, [[opId, projectId, sceneId, status, …]]]``.

    Ojo: el tercer uuid es la **escena**, no el media. Leerlo como media id es
    lo que hacía que toda búsqueda ``as29s`` respondiera NOT_FOUND.
    """
    records = payload[2] if isinstance(payload, list) and len(payload) > 2 else None
    record = records[0] if isinstance(records, list) and records else None
    if not isinstance(record, list) or not record:
        raise FlowBatchError("operation payload carried no record")
    return Operation(
        operation_id=record[0],
        project_id=record[1] if len(record) > 1 else None,
        status=record[3] if len(record) > 3 else None,
        error=read_operation_error(record),
    )


def find_media_id(payload: Any, operation_id: str) -> Optional[str]:
    """Busca la operación en el listado del proyecto y toma su media id."""
    for node in _walk_lists(payload):
        if len(node) < 4 or node[0] != operation_id:
            continue
        detail = node[3]
        if isinstance(detail, list) and len(detail) > 4 and isinstance(detail[4], str):
            return detail[4]
    return None


#: Slot del media en una entrada de listado, matcheado crudo: título, par de
#: timestamps, dos nulls, y el media id.
_MEDIA_SLOT = re.compile(r'null,null,\\?"([0-9a-fA-F-]{36})\\?"')


def find_media_id_in_text(text: str, operation_id: str) -> Optional[str]:
    """Igual que :func:`find_media_id` pero sobre un listado sin parsear."""
    start = text.find(operation_id)
    if start == -1:
        return None
    match = _MEDIA_SLOT.search(text, start, start + 800)
    return match.group(1) if match else None


def read_media_urls(payload: Any, media_id: str) -> MediaUrls:
    video = image = None
    for text in _walk_strings(payload):
        if not text.startswith("https://"):
            continue
        if MEDIA_HOST + "/video/" in text and video is None:
            video = text
        elif MEDIA_HOST + "/image/" in text and image is None:
            image = text
    return MediaUrls(media_id=media_id, video=video, image=image)


# ══════════════════════════════════════════════════════════════════════════
#  Lado página: tokens, RPC y proyectos
# ══════════════════════════════════════════════════════════════════════════

_TOKENS_JS = """
() => {
    const w = globalThis.WIZ_global_data || {};
    return {
        at: w.SNlM0e || null,
        sid: w.FdrFJe || null,
        bl: w.cfb2h || null,
        path: location.pathname,
        host: location.host,
        project: (location.pathname.match(/\\/project\\/([0-9a-fA-F-]{36})/) || [])[1] || null,
    };
}
"""

_RPC_JS = """
async ([rpcid, freq, maxChars]) => {
    const wiz = globalThis.WIZ_global_data || {};
    const at = wiz.SNlM0e;
    const sid = wiz.FdrFJe;
    const bl = wiz.cfb2h;
    if (!at) return { error: 'NO_AT_TOKEN' };
    const reqid = Math.floor(Math.random() * 900000) + 100000;
    // Flow matchea su propio metadata de WIZ; GEM_PIX_2 rechaza la generación
    // de imagen si falta source-path.
    const sourcePath = location.pathname || '/';
    const hl = (document.documentElement.lang || navigator.language || 'en').split('-')[0];
    const url = '/_/AiSandboxAngularFrontend/data/batchexecute?rpcids=' + encodeURIComponent(rpcid) +
        '&source-path=' + encodeURIComponent(sourcePath) +
        '&bl=' + encodeURIComponent(bl || '') + '&f.sid=' + encodeURIComponent(sid || '') +
        '&hl=' + encodeURIComponent(hl) + '&_reqid=' + reqid + '&rt=c';
    try {
        const resp = await fetch(url, {
            method: 'POST',
            credentials: 'include',
            headers: {
                'content-type': 'application/x-www-form-urlencoded;charset=UTF-8',
                'x-same-domain': '1',
            },
            body: new URLSearchParams({ 'f.req': freq, at }),
        });
        const text = await resp.text();
        return { status: resp.status, text: text.length > maxChars ? text.slice(0, maxChars) : text };
    } catch (e) {
        return { error: 'FETCH_FAILED: ' + (e && e.message ? e.message : e) };
    }
}
"""


async def page_tokens(page: Page) -> dict:
    """Lee ``at`` / ``f.sid`` / ``bl`` y el proyecto actual de la página de Flow."""
    info = await page.evaluate(_TOKENS_JS)
    if not isinstance(info, dict) or not info.get("at"):
        raise NoAtTokenError(
            "Flow page did not expose WIZ_global_data.SNlM0e — navega a "
            f"{FLOW_HOME} con la sesión iniciada y reintenta."
        )
    return info


async def wait_for_flow_ready(page: Page, *, timeout_ms: int = 45_000, poll_ms: int = 1_500) -> dict:
    """Garantiza una pestaña de Flow viva con tokens: navega y espera ``at``."""
    if "flow.google.com" not in (page.url or ""):
        try:
            await page.goto(FLOW_HOME, wait_until="domcontentloaded",
                            timeout=min(timeout_ms, 60_000))
        except Exception as exc:  # la página puede quedar usable igual
            log.warning("flow.navigation", error=str(exc))
    deadline = time.monotonic() + timeout_ms / 1000
    last: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            return await page_tokens(page)
        except NoAtTokenError as exc:
            last = exc
        except Exception as exc:  # navegación en curso
            last = exc
        await page.wait_for_timeout(poll_ms)
    raise NoAtTokenError(str(last) if last else "Flow page never became ready")


_READY_JS = """
() => ({
    at: (globalThis.WIZ_global_data || {}).SNlM0e || null,
    grecaptcha: !!(window.grecaptcha && window.grecaptcha.enterprise
                   && window.grecaptcha.enterprise.execute),
})
"""


async def open_project(page: Page, project_id: str, *, timeout_ms: int = 60_000,
                       poll_ms: int = 1_500) -> dict:
    """Navega a la página del proyecto y espera tokens ``at`` + grecaptcha.

    Es la única vista desde la que se puede generar: la portada no carga el
    cliente de reCAPTCHA y el ``source-path`` del RPC debe ser el del proyecto.
    """
    url = PROJECT_URL.format(pid=project_id)
    if url not in (page.url or ""):
        try:
            await page.goto(url, wait_until="domcontentloaded",
                            timeout=min(timeout_ms, 60_000))
        except Exception as exc:
            log.warning("flow.navigation", error=str(exc))
    deadline = time.monotonic() + timeout_ms / 1000
    last: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            state = await page.evaluate(_READY_JS)
        except Exception as exc:  # navegando
            state, last = None, exc
        if isinstance(state, dict) and state.get("at") and state.get("grecaptcha"):
            return state
        await page.wait_for_timeout(poll_ms)
    raise NoAtTokenError(
        f"project page never became ready (at+grecaptcha): {last or 'timeout'}"
    )


def apply_captcha(freq: str, token: Optional[str]) -> str:
    """Sustituye el marcador de captcha; sin token manda vacío (polls lo ignoran)."""
    if CAPTCHA_SLOT not in freq:
        return freq
    return freq.replace(CAPTCHA_SLOT, token or "")


async def run_rpc(
    page: Page,
    rpcid: str,
    freq: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> tuple[int, str]:
    """Ejecuta un ``f.req`` dentro de la pestaña de Flow y devuelve (status, texto)."""
    out = await page.evaluate(_RPC_JS, [rpcid, freq, max_chars])
    if not isinstance(out, dict):
        raise FlowBatchError(f"unexpected page result for {rpcid}: {out!r}")
    if out.get("error"):
        if out["error"] == "NO_AT_TOKEN":
            raise NoAtTokenError("WIZ_global_data.SNlM0e is gone mid-call")
        raise FlowBatchError(str(out["error"]))
    status = int(out.get("status") or 0)
    text = out.get("text") or ""
    if status != 200:
        raise FlowBatchError(f"{rpcid} returned HTTP {status}: {text[:300]}")
    return status, text


async def rpc_payload(page: Page, rpcid: str, freq: str, *,
                      max_chars: int = DEFAULT_MAX_CHARS) -> Any:
    """Corre un RPC y devuelve el payload ya parseado del envelope."""
    _, text = await run_rpc(page, rpcid, freq, max_chars=max_chars)
    return first_payload(text, rpcid)


async def current_project_id(page: Page) -> Optional[str]:
    """El proyecto de la pestaña, o el guardado en estado/entorno."""
    env_pid = os.environ.get("FLOW_PROJECT_ID") or stored_project_id()
    try:
        info = await page.evaluate(_TOKENS_JS)
    except Exception:
        return env_pid
    if isinstance(info, dict) and info.get("project"):
        return str(info["project"])
    return env_pid


# ── Estado local: reusar el mismo proyecto en vez de llenar la cuenta ─────


def _state_path():
    from pathlib import Path

    return Path.home() / ".flow-mcp" / "state.json"


def stored_project_id() -> Optional[str]:
    """Lee ``~/.flow-mcp/state.json`` (el proyecto por defecto de este host)."""
    import json as _json

    path = _state_path()
    try:
        data = _json.loads(path.read_text())
    except Exception:
        return None
    pid = data.get("project_id") if isinstance(data, dict) else None
    return pid if isinstance(pid, str) and pid else None


def store_project_id(project_id: str) -> None:
    """Guarda el proyecto por defecto (para no crear basura en cada corrida)."""
    import json as _json

    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps({"project_id": project_id}, indent=2) + "\n")
    except Exception as exc:
        log.warning("project.persist_failed", error=str(exc))


async def create_project(page: Page, title: str = "flow-mcp project") -> str:
    """Crea un proyecto por batchexecute, lo guarda y devuelve su id."""
    payload = await rpc_payload(page, RPC_CREATE_PROJECT, create_project_request(title))
    project_id, _ = read_created_project(payload)
    log.info("project.created", pid=project_id)
    return project_id


async def ensure_project(page: Page, title: str = "flow-mcp project") -> str:
    """Proyecto actual de la pestaña → ``FLOW_PROJECT_ID``/estado → crear uno.

    Toda llamada de generación está scopeada a un proyecto: sin uno, Flow
    rechaza el RPC. El creado queda guardado para reusarlo.
    """
    pid = await current_project_id(page)
    if pid:
        return pid
    pid = await create_project(page, title)
    store_project_id(pid)
    return pid
