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
            self._site_key = await discover_site_key(self._page)
        return self._site_key

    async def mint(self, action: str) -> str:
        """Mint un token reCAPTCHA Enterprise fresco.

        Los tokens son de un solo uso y expiran en ~2 minutos.
        """
        site_key = await self.site_key()
        try:
            token = await self._page.evaluate(_EXECUTE_JS, [site_key, action])
        except Exception as exc:
            raise RecaptchaError(
                f"reCAPTCHA evaluate failed for action={action!r}: {exc}. "
                "Likely causes: grecaptcha not loaded or page navigated away."
            ) from exc
        if not isinstance(token, str) or not token:
            raise RecaptchaError(
                f"reCAPTCHA returned an empty token for action={action!r}."
            )
        log.info("recaptcha.minted", action=action)
        return token
