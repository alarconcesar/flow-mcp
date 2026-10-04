"""reCAPTCHA Enterprise token minting via Playwright page.evaluate.

Portado de gflow-cli para eliminar la dependencia externa.
Descubre el site key del script reCAPTCHA en la página y ejecuta
``grecaptcha.enterprise.execute()`` para mintear un token.
"""

from __future__ import annotations

from typing import Any, Protocol

import structlog

log = structlog.get_logger("flow-mcp")


class _PageLike(Protocol):
    """Mínimo subset de playwright.async_api.Page que necesitamos."""

    async def evaluate(self, expression: str, arg: Any = None) -> Any: ...


class RecaptchaError(RuntimeError):
    """Error al mintear token reCAPTCHA."""


_DISCOVER_SITE_KEY_JS = """
() => {
    const scripts = document.querySelectorAll('script[src*="recaptcha/enterprise.js"]');
    for (const s of scripts) {
        const m = (s.getAttribute('src') || '').match(/[?&]render=([^&]+)/);
        if (m) return m[1];
    }
    return null;
}
"""

_EXECUTE_JS = """
async ([siteKey, action]) => {
    return await new Promise((resolve, reject) => {
        if (typeof grecaptcha === 'undefined' || !grecaptcha.enterprise) {
            return reject(new Error('grecaptcha.enterprise not loaded'));
        }
        grecaptcha.enterprise.ready(() => {
            grecaptcha.enterprise
                .execute(siteKey, { action })
                .then(resolve)
                .catch(reject);
        });
    });
}
"""

#: Site key de Flow (verificado empíricamente oct-2026 en flow.google.com). Se
#: expone también en el tag ``enterprise.js?render=…`` de la página de proyecto;
#: esto es solo el respaldo si la página aún no lo cargó.
FALLBACK_SITE_KEY = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"

#: Mint con bypass. La app nueva (sep-2026) envuelve
#: ``grecaptcha.enterprise.execute`` con un ``Object.assign`` que fuerza
#: ``action: "extension_hijack_detected"``: cualquier token emitido por la API
#: pública sale envenenado y Flow lo rechaza. Neutralizamos ese assign
#: únicamente durante la llamada y restauramos el action real.
_MINT_JS = """
async ([siteKey, action]) => {
    const g = window.grecaptcha && window.grecaptcha.enterprise;
    if (!g || typeof g.execute !== 'function') return { error: 'NO_GRECAPTCHA' };
    const realAssign = Object.assign;
    Object.assign = function (target, ...sources) {
        const out = realAssign(target, ...sources);
        try {
            if (out && typeof out === 'object' && out.action === 'extension_hijack_detected') {
                out.action = action;
            }
        } catch (e) { /* ignore */ }
        return out;
    };
    const run = () => g.execute(siteKey, { action });
    try {
        const token = await new Promise((resolve, reject) => {
            let settled = false;
            const ok = (v) => { if (!settled) { settled = true; resolve(v); } };
            const ko = (e) => { if (!settled) { settled = true; reject(e); } };
            try {
                if (typeof g.ready === 'function') g.ready(() => run().then(ok).catch(ko));
                else run().then(ok).catch(ko);
            } catch (e) { ko(e); }
        });
        return { token };
    } catch (e) {
        return { error: 'EXECUTE_FAILED: ' + (e && e.message ? e.message : e) };
    } finally {
        Object.assign = realAssign;
    }
}
"""


async def discover_site_key(page: _PageLike) -> str:
    """Lee el site key de reCAPTCHA Enterprise desde la página cargada."""
    key = await page.evaluate(_DISCOVER_SITE_KEY_JS)
    if not isinstance(key, str) or not key:
        raise RecaptchaError(
            "Could not discover reCAPTCHA site key from the page. "
            "The Flow editor page may have failed to load."
        )
    log.debug("recaptcha.site_key_discovered")
    return key


class TokenMinter:
    """Mint reCAPTCHA tokens. Cachea el site key por sesión."""

    def __init__(self, page: _PageLike) -> None:
        self._page = page
        self._site_key: str | None = None

    async def site_key(self) -> str:
        if self._site_key is None:
            try:
                self._site_key = await discover_site_key(self._page)
            except RecaptchaError:
                # La página de proyecto expone el key en el tag
                # ``enterprise.js?render=…``, pero la portada no lo carga.
                log.warning("recaptcha.site_key_fallback")
                self._site_key = FALLBACK_SITE_KEY
        return self._site_key

    async def mint(self, action: str) -> str:
        """Mint un token reCAPTCHA Enterprise fresco (con bypass del poison).

        Los tokens son de un solo uso y expiran en ~2 minutos: se mintea uno
        por request, momentos antes de que salga.
        """
        site_key = await self.site_key()
        try:
            out = await self._page.evaluate(_MINT_JS, [site_key, action])
        except Exception as exc:
            raise RecaptchaError(
                f"reCAPTCHA evaluate failed for action={action!r}: {exc}. "
                "Likely causes: grecaptcha not loaded or page navigated away."
            ) from exc
        if isinstance(out, dict):
            token = out.get("token")
            if isinstance(token, str) and token:
                log.info("recaptcha.minted", action=action)
                return token
            if not out.get("error"):
                raise RecaptchaError(
                    f"reCAPTCHA returned an empty token for action={action!r}."
                )
            raise RecaptchaError(
                f"reCAPTCHA mint failed for action={action!r}: {out['error']}"
            )
        raise RecaptchaError(
            f"reCAPTCHA mint returned an unexpected payload for action={action!r}: {out!r}"
        )
