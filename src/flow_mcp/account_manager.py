"""Account manager — multi-account support with automatic fallback.

Allows configuring multiple Google Flow accounts. When one account runs
out of credits, the system automatically switches to the next one.

Configuration
-------------
Ordered account list via env var (comma-separated profile names)::

    GFLOW_ACCOUNTS=cesar,cuenta2,cuenta3

If unset, all authenticated profiles are used (in filesystem order).
The first account in the list is the default.

Per-account state is tracked via an ``.gflow_account_priority`` file that
stores the order index, so the pool remembers which account to use even
after restart.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import ClassVar

import structlog

from flow_mcp.profile import (
    _find_authenticated_profiles,
    _list_profiles,
    _profile_name_from_dir,
    default_home,
)

log = structlog.get_logger("flow-mcp")

_STATE_FILE = ".gflow_active_account"


class AccountCycleError(RuntimeError):
    """All accounts have been exhausted."""


class AccountManager:
    """Manages a prioritized list of Flow accounts with automatic fallback.

    Usage::

        mgr = AccountManager()
        profile_dir = mgr.current_profile_dir()   # first/current account
        # ... generation fails with no credits ...
        mgr.switch_to_next()                       # move to next account
        profile_dir = mgr.current_profile_dir()   # next account's dir
    """

    _instance: ClassVar[AccountManager | None] = None

    def __init__(self) -> None:
        self._home = default_home()
        self._accounts: list[str] = []
        self._current: int = 0
        self._load_accounts()
        self._load_state()
        log.info(
            "account_manager.init",
            accounts=self._accounts,
            current=self._current,
            active=self.active_name,
        )

    # ── Public API ──────────────────────────────────────────────────────

    @property
    def active_name(self) -> str | None:
        """Return the currently active account name, or ``None``."""
        if not self._accounts:
            return None
        return self._accounts[self._current]

    @property
    def active_dir(self) -> Path | None:
        """Return the profile directory for the active account."""
        name = self.active_name
        if name is None:
            return None
        return self._home / f"profile_{name}"

    @property
    def account_count(self) -> int:
        return len(self._accounts)

    @property
    def all_accounts(self) -> list[str]:
        return list(self._accounts)

    def current_profile_dir(self) -> Path:
        """Return the profile dir for the active account.

        Raises ``RuntimeError`` if no accounts are configured.
        """
        d = self.active_dir
        if d is None:
            raise RuntimeError(
                "No Flow accounts configured. "
                "Run `flow-mcp auth login` to add one, "
                "or set GFLOW_ACCOUNTS=name1,name2 in env."
            )
        return d

    def switch_to_next(self) -> str | None:
        """Move to the next account in the list.

        Returns the new account name, or ``None`` if we've exhausted all
        accounts and wrapped around to the first.
        """
        if not self._accounts:
            return None

        self._current += 1
        if self._current >= len(self._accounts):
            self._current = 0
            self._save_state()
            raise AccountCycleError(
                "All accounts have been exhausted. "
                f"Tried: {', '.join(self._accounts)}"
            )

        name = self._accounts[self._current]
        self._save_state()
        log.info("account_manager.switched", new_account=name)
        return name

    def reset(self) -> None:
        """Reset to the first account."""
        self._current = 0
        self._save_state()
        log.info("account_manager.reset", account=self.active_name)

    # ── Internal ────────────────────────────────────────────────────────

    def _load_accounts(self) -> None:
        """Load the ordered list of accounts.

        Priority:
        1. ``GFLOW_ACCOUNTS`` env var (comma-separated profile names)
        2. All authenticated profiles (any with ``.gflow_account``)
        3. Fallback to ``default``
        """
        env = os.environ.get("GFLOW_ACCOUNTS", "").strip()
        if env:
            names = [n.strip() for n in env.split(",") if n.strip()]
            if names:
                self._accounts = names
                log.info("account_manager.from_env", accounts=names)
                return

        # Auto-detect all authenticated profiles
        authed = _find_authenticated_profiles(self._home)
        if authed:
            self._accounts = authed
            log.info("account_manager.auto_detected", accounts=authed)
            return

        # Fallback — ensure at least "default" exists if directory present
        profiles = _list_profiles(self._home)
        if profiles:
            self._accounts = [_profile_name_from_dir(p) for p in profiles]
            log.info("account_manager.all_profiles", accounts=self._accounts)
            return

        # Last resort — just set "default" (will error later if not found)
        self._accounts = ["default"]
        log.info("account_manager.fallback_default")

    def _load_state(self) -> None:
        """Load the last active account index from disk."""
        state_path = self._home / _STATE_FILE
        if state_path.exists():
            try:
                saved = state_path.read_text(encoding="utf-8").strip()
                if saved in self._accounts:
                    self._current = self._accounts.index(saved)
                    log.info("account_manager.state_loaded", account=saved)
            except OSError:
                pass

    def _save_state(self) -> None:
        """Persist the active account name to disk."""
        name = self.active_name
        if name:
            try:
                state_path = self._home / _STATE_FILE
                self._home.mkdir(parents=True, exist_ok=True)
                state_path.write_text(name, encoding="utf-8")
            except OSError:
                pass

    # ── Singleton ───────────────────────────────────────────────────────

    @classmethod
    def get_instance(cls) -> AccountManager:
        """Return the module-level singleton."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Clear the singleton (for testing)."""
        cls._instance = None


# ── Convenience functions ─────────────────────────────────────────────────
# These mirror the old resolve_profile() API so existing code works.


def resolve_active_profile() -> Path:
    """Return the profile dir for the active account (replaces resolve_profile)."""
    return AccountManager.get_instance().current_profile_dir()


def switch_account() -> str | None:
    """Switch to the next account. Returns the new name or raises."""
    return AccountManager.get_instance().switch_to_next()


def reset_accounts() -> None:
    """Reset to first account."""
    AccountManager.get_instance().reset()
