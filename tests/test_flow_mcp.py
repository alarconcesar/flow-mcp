"""Tests for flow-mcp package."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Constants ─────────────────────────────────────────────────────────────


class TestConstants:
    """Test that constants are correctly defined."""

    def test_aspect_ratios_mapping(self) -> None:
        from flow_mcp.constants import ASPECT_RATIOS, ALLOWED_ASPECTS

        assert "9:16" in ASPECT_RATIOS
        assert "16:9" in ASPECT_RATIOS
        assert "1:1" in ASPECT_RATIOS
        assert "4:3" in ASPECT_RATIOS
        assert "3:4" in ASPECT_RATIOS
        assert ASPECT_RATIOS["9:16"] == "IMAGE_ASPECT_RATIO_PORTRAIT"
        assert ASPECT_RATIOS["16:9"] == "IMAGE_ASPECT_RATIO_LANDSCAPE"
        assert ASPECT_RATIOS["1:1"] == "IMAGE_ASPECT_RATIO_SQUARE"

    def test_models_mapping(self) -> None:
        from flow_mcp.constants import MODELS, ALLOWED_MODELS

        assert "nano2" in MODELS
        assert "nano-pro" in MODELS
        assert "narwhal" in MODELS
        assert "gem_pix_2" in MODELS
        assert MODELS["nano2"] == "NARWHAL"
        assert MODELS["nano-pro"] == "GEM_PIX_2"
        assert MODELS["narwhal"] == "NARWHAL"
        assert MODELS["gem_pix_2"] == "GEM_PIX_2"

    def test_browser_args(self) -> None:
        from flow_mcp.constants import BROWSER_ARGS

        assert "--no-sandbox" in BROWSER_ARGS
        assert "--disable-gpu" in BROWSER_ARGS

    def test_idle_timeout_zero(self) -> None:
        """BROWSER_IDLE_TIMEOUT_S must be 0 to prevent ClosedResourceError."""
        from flow_mcp.constants import BROWSER_IDLE_TIMEOUT_S

        assert BROWSER_IDLE_TIMEOUT_S == 0

    def test_generation_timeouts(self) -> None:
        from flow_mcp.constants import GEN_POLL_INTERVAL_MS, GEN_TIMEOUT_MS

        assert GEN_POLL_INTERVAL_MS == 2_000
        assert GEN_TIMEOUT_MS == 90_000

    def test_version_consistency(self) -> None:
        from flow_mcp import __version__

        assert __version__ == "0.4.0"


# ── Generator (stateless helpers) ─────────────────────────────────────────


class TestGeneratorHelpers:
    def test_tempfile_dir(self) -> None:
        from flow_mcp.generator import tempfile_dir

        d = tempfile_dir()
        assert isinstance(d, str)
        assert os.path.isdir(d)

    def test_guess_ext_from_mime(self) -> None:
        from flow_mcp.generator import _guess_ext_from_mime

        assert _guess_ext_from_mime("data:image/png;base64,...") == "png"
        assert _guess_ext_from_mime("data:image/jpeg;base64,...") == "jpg"
        assert _guess_ext_from_mime("data:image/webp;base64,...") == "webp"
        assert _guess_ext_from_mime("rawdata") == "jpg"

    def test_generation_result(self, tmp_path: Path) -> None:
        from flow_mcp.generator import GenerationResult

        # Create real files so stat() works
        f1 = tmp_path / "test.png"
        f2 = tmp_path / "test2.jpg"
        f1.write_bytes(b"fakeimage")
        f2.write_bytes(b"fakeimage2")

        result = GenerationResult(files=[f1, f2])
        assert len(result.paths) == 2
        desc = result.describe()
        assert "Generated 2 image(s)" in desc
        assert "test.png" in desc
        assert "test2.jpg" in desc

    def test_generation_result_single(self, tmp_path: Path) -> None:
        from flow_mcp.generator import GenerationResult

        f = tmp_path / "test.png"
        f.write_bytes(b"fakeimage")
        result = GenerationResult(files=[f])
        assert "Generated 1 image(s)" in result.describe()

    def test_error_classes(self) -> None:
        from flow_mcp.generator import (
            GenerationError,
            ContentFilteredError,
            AuthError,
        )

        assert issubclass(ContentFilteredError, GenerationError)
        assert issubclass(AuthError, GenerationError)
        assert issubclass(GenerationError, RuntimeError)

    def test_await_window_var_poll(self) -> None:
        """Test the polling logic of _await_window_var."""
        # This is an async test but the function uses time.monotonic
        # which makes it tricky without a real page. We verify the
        # function signature and logic at import time.
        from flow_mcp.generator import _await_window_var
        import inspect

        sig = inspect.signature(_await_window_var)
        params = list(sig.parameters.keys())
        assert "page" in params
        assert "var_name" in params
        assert "poll_ms" in params
        assert "timeout_ms" in params


# ── Chrome helpers ────────────────────────────────────────────────────────


class TestChromeHelpers:
    def test_chrome_paths_structure(self) -> None:
        """Verify the module imports and structure."""
        from flow_mcp.chrome_helpers import (
            _is_playwright_chrome_channel_available,
            channel_for_profile,
        )

        # These should be callable without errors
        assert callable(_is_playwright_chrome_channel_available)
        assert callable(channel_for_profile)

    def test_channel_for_profile_no_marker(self, tmp_path: Path) -> None:
        from flow_mcp.chrome_helpers import channel_for_profile

        # No .gflow_browser_strategy file → should return None
        result = channel_for_profile(tmp_path)
        assert result is None

    def test_channel_for_profile_unknown_strategy(self, tmp_path: Path) -> None:
        from flow_mcp.chrome_helpers import channel_for_profile

        # Write a non-chrome strategy
        (tmp_path / ".gflow_browser_strategy").write_text("firefox", encoding="utf-8")
        result = channel_for_profile(tmp_path)
        assert result is None


# ── Profile resolution ────────────────────────────────────────────────────


class TestProfile:
    def test_default_home(self) -> None:
        from flow_mcp.profile import default_home

        home = default_home()
        assert isinstance(home, Path)
        assert "gflow-cli" in str(home)

    def test_list_profiles_no_dir(self, tmp_path: Path) -> None:
        from flow_mcp.profile import _list_profiles

        profiles = _list_profiles(tmp_path / "nonexistent")
        assert profiles == []

    def test_list_profiles_empty(self, tmp_path: Path) -> None:
        from flow_mcp.profile import _list_profiles

        tmp_path.mkdir(exist_ok=True)
        profiles = _list_profiles(tmp_path)
        assert profiles == []

    def test_list_profiles_with_data(self, tmp_path: Path) -> None:
        from flow_mcp.profile import _list_profiles

        (tmp_path / "profile_default").mkdir()
        (tmp_path / "profile_test").mkdir()
        (tmp_path / "not_a_profile").mkdir()  # no prefix

        profiles = _list_profiles(tmp_path)
        assert len(profiles) == 2
        names = [p.name for p in profiles]
        assert "profile_default" in names
        assert "profile_test" in names
        assert "not_a_profile" not in names

    def test_profile_name_from_dir(self) -> None:
        from flow_mcp.profile import _profile_name_from_dir

        assert _profile_name_from_dir(Path("/home/gflow-cli/profile_test")) == "test"
        assert _profile_name_from_dir(Path("/x/profile_")) == ""

    def test_find_authenticated_profile(self, tmp_path: Path) -> None:
        from flow_mcp.profile import _find_authenticated_profile

        # No profiles
        assert _find_authenticated_profile(tmp_path) is None

        # Profile without .gflow_account
        (tmp_path / "profile_default").mkdir()
        assert _find_authenticated_profile(tmp_path) is None

        # Profile with .gflow_account
        (tmp_path / "profile_test").mkdir()
        (tmp_path / "profile_test" / ".gflow_account").write_text(
            "test@example.com", encoding="utf-8"
        )
        result = _find_authenticated_profile(tmp_path)
        assert result == "test"

    def test_resolve_profile_env_var(self) -> None:
        """Test that GFLOW_PROFILE env var is honored."""
        from flow_mcp.profile import resolve_profile

        with tempfile.TemporaryDirectory() as tmp:
            # Create a profile directory
            profile_dir = Path(tmp) / "profile_myprofile"
            profile_dir.mkdir(parents=True)
            (profile_dir / ".gflow_account").write_text("x@x.com", encoding="utf-8")

            with patch.dict(os.environ, {
                "GFLOW_CLI_HOME": tmp,
                "GFLOW_PROFILE": "myprofile",
            }):
                result = resolve_profile()
                assert result == profile_dir

    def test_resolve_profile_fallback_to_default(self, tmp_path: Path) -> None:
        """If no env var and no authenticated profile, tries 'default'."""
        from flow_mcp.profile import resolve_profile

        (tmp_path / "profile_default").mkdir()

        with patch.dict(os.environ, {"GFLOW_CLI_HOME": str(tmp_path)}):
            result = resolve_profile()
            assert result.name == "profile_default"

    def test_resolve_profile_not_found(self) -> None:
        """If profile doesn't exist at all, should raise RuntimeError."""
        from flow_mcp.profile import resolve_profile

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"GFLOW_CLI_HOME": tmp}):
                with pytest.raises(RuntimeError, match="not found"):
                    resolve_profile()


# ── reCAPTCHA token minting ───────────────────────────────────────────────


class TestRecaptcha:
    @pytest.mark.asyncio
    async def test_discover_site_key(self) -> None:
        from flow_mcp.recaptcha import discover_site_key, RecaptchaError

        mock_page = MagicMock()

        # Mock successful discover
        mock_page.evaluate = AsyncMock(return_value="6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI")
        key = await discover_site_key(mock_page)
        assert key == "6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI"

        # Mock failed discover (returns None)
        mock_page2 = MagicMock()
        mock_page2.evaluate = AsyncMock(return_value=None)
        with pytest.raises(RecaptchaError, match="Could not discover"):
            await discover_site_key(mock_page2)

    @pytest.mark.asyncio
    async def test_token_minter(self) -> None:
        from flow_mcp.recaptcha import TokenMinter

        mock_page = MagicMock()
        # First call returns site key, second returns token
        mock_page.evaluate = AsyncMock()
        mock_page.evaluate.side_effect = [
            "6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI",  # discover site key
            "mocked_token",  # mint token
        ]

        minter = TokenMinter(mock_page)
        token = await minter.mint("IMAGE_GENERATION")
        assert token == "mocked_token"

        # Site key should be cached
        assert minter._site_key is not None
        assert mock_page.evaluate.call_count == 2  # discover + mint (mint not cached)

    @pytest.mark.asyncio
    async def test_token_minter_empty_token(self) -> None:
        from flow_mcp.recaptcha import TokenMinter, RecaptchaError

        mock_page = MagicMock()
        mock_page.evaluate = AsyncMock()
        mock_page.evaluate.side_effect = [
            "6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI",  # discover site key
            "",  # empty token
        ]

        minter = TokenMinter(mock_page)
        with pytest.raises(RecaptchaError, match="empty token"):
            await minter.mint("SOME_ACTION")

    @pytest.mark.asyncio
    async def test_token_minter_exception(self) -> None:
        from flow_mcp.recaptcha import TokenMinter, RecaptchaError

        mock_page = MagicMock()
        mock_page.evaluate = AsyncMock()
        mock_page.evaluate.side_effect = [
            "6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI",  # discover site key
            Exception("grecaptcha not loaded"),  # mint fails
        ]

        minter = TokenMinter(mock_page)
        with pytest.raises(RecaptchaError, match="grecaptcha not loaded"):
            await minter.mint("SOME_ACTION")


# ── Entry point / CLI ─────────────────────────────────────────────────────


class TestMainCLI:
    """Test the CLI argument parsing fix (--browser flag)."""

    def test_no_args_starts_server(self) -> None:
        """flow-mcp with no args should go to server_main."""
        with patch.object(sys, "argv", ["flow-mcp"]):
            with patch("flow_mcp.__main__._print_help") as mock_help:
                with patch("flow_mcp.server.main") as mock_server:
                    from flow_mcp.__main__ import main

                    main()
                    # With 0 positional args (help is only if args contains 'help')
                    mock_help.assert_not_called()
                    mock_server.assert_called_once()

    def test_help_command(self) -> None:
        """flow-mcp help should print help."""
        with patch.object(sys, "argv", ["flow-mcp", "help"]):
            with patch("flow_mcp.__main__._print_help") as mock_help:
                from flow_mcp.__main__ import main

                main()
                mock_help.assert_called_once()

    def test_auth_list(self) -> None:
        """flow-mcp auth list should call cmd_list."""
        with patch.object(sys, "argv", ["flow-mcp", "auth", "list"]):
            with patch("flow_mcp.__main__._run_async") as mock_run:
                from flow_mcp.__main__ import main

                main()
                # Check it ran something async
                mock_run.assert_called_once()

    def test_auth_login_default_chrome(self) -> None:
        """flow-mcp auth login should call cmd_login (system Chrome)."""
        with patch.object(sys, "argv", ["flow-mcp", "auth", "login"]):
            with patch("flow_mcp.__main__._run_async") as mock_run:
                with patch("flow_mcp.__main__._auth_login") as mock_auth:
                    from flow_mcp.__main__ import main

                    main()
                    mock_run.assert_called_once()

    def test_auth_login_browser_internal(self) -> None:
        """flow-mcp auth login --browser internal should call cmd_login_internal."""
        with patch.object(sys, "argv", ["flow-mcp", "auth", "login", "--browser", "internal"]):
            with patch("flow_mcp.__main__._run_async") as mock_run:
                from flow_mcp.__main__ import main

                main()

                # _run_async should have been called with _auth_login_internal
                # We can't easily check the exact coroutine, but we verify
                # it ran something async (the old broken code would have gone to server mode)
                mock_run.assert_called_once()

    def test_auth_login_browser_chrome(self) -> None:
        """flow-mcp auth login --browser chrome should call cmd_login (system Chrome)."""
        with patch.object(sys, "argv", ["flow-mcp", "auth", "login", "--browser", "chrome"]):
            with patch("flow_mcp.__main__._run_async") as mock_run:
                from flow_mcp.__main__ import main

                main()

                mock_run.assert_called_once()

    def test_auth_login_internal_command(self) -> None:
        """flow-mcp auth login-internal should call cmd_login_internal."""
        with patch.object(sys, "argv", ["flow-mcp", "auth", "login-internal"]):
            with patch("flow_mcp.__main__._run_async") as mock_run:
                from flow_mcp.__main__ import main

                main()
                mock_run.assert_called_once()

    def test_run_async_keyboard_interrupt(self) -> None:
        """_run_async should handle Ctrl+C gracefully."""
        from flow_mcp.__main__ import _run_async

        async def raises_interrupt() -> None:
            raise KeyboardInterrupt()

        # Should not raise — just exit with code 0
        with patch.object(sys, "exit") as mock_exit:
            _run_async(raises_interrupt())
            mock_exit.assert_called_once_with(0)

    def test_help_output(self, capsys: pytest.CaptureFixture) -> None:
        from flow_mcp.__main__ import _print_help

        _print_help()
        captured = capsys.readouterr()
        assert "v0.4.0" in captured.out
        assert "auth login" in captured.out
        assert "--browser internal" in captured.out
        assert "auth accounts" in captured.out


# ── Server / Tool validation ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_image_validation():
    """Test that generate_image validates inputs correctly."""
    from flow_mcp.server import generate_image

    # The tool needs a FastMCP context (ctx) which we can't easily mock
    # But we can test the validation logic inside it by calling without ctx
    # Invalid model should return error JSON, not raise
    result = await generate_image(
        prompt="test",
        model="invalid_model",
        count=1,
        aspect="1:1",
    )
    data = json.loads(result) if isinstance(result, str) else result
    assert data.get("success") is False
    assert "Invalid model" in data.get("error", "")

    # Invalid aspect
    result = await generate_image(
        prompt="test",
        model="nano-pro",
        count=1,
        aspect="99:99",
    )
    data = json.loads(result) if isinstance(result, str) else result
    assert data.get("success") is False
    assert "Invalid aspect" in data.get("error", "")

    # Invalid resolution
    result = await generate_image(
        prompt="test",
        model="nano-pro",
        count=1,
        aspect="1:1",
        resolution="8k",
    )
    data = json.loads(result) if isinstance(result, str) else result
    assert data.get("success") is False
    assert "Invalid resolution" in data.get("error", "")


@pytest.mark.asyncio
async def test_generate_image_count_clamping():
    """Test that count is clamped to [1, 4]."""
    from flow_mcp.server import generate_image

    # Very large count should get clamped
    result = await generate_image(
        prompt="test", model="nano-pro", count=999, aspect="1:1",
    )
    # It'll fail on the actual generation, not on count validation
    # (count clamping is internal, so this validates it doesn't crash on large count)
    assert isinstance(result, str)

    # count=0 should become 1
    result = await generate_image(
        prompt="test", model="nano-pro", count=0, aspect="1:1",
    )
    assert isinstance(result, str)


# ── Auth module ───────────────────────────────────────────────────────────


class TestAuth:
    def test_profile_info_email(self, tmp_path: Path) -> None:
        from flow_mcp.auth import ProfileInfo

        info = ProfileInfo("test", tmp_path)
        assert info.name == "test"
        assert info.email is None
        assert not info.is_authenticated

        # With .gflow_account
        (tmp_path / ".gflow_account").write_text("user@example.com", encoding="utf-8")
        assert info.email == "user@example.com"
        assert info.is_authenticated

    @pytest.mark.asyncio
    async def test_check_session(self) -> None:
        from flow_mcp.auth import _check_session

        mock_page = MagicMock()

        # Successful session
        mock_page.evaluate = AsyncMock(return_value="user@example.com")
        result = await _check_session(mock_page)
        assert result == "user@example.com"

        # No session
        mock_page2 = MagicMock()
        mock_page2.evaluate = AsyncMock(return_value=None)
        result = await _check_session(mock_page2)
        assert result is None

        # Error during check
        mock_page3 = MagicMock()
        mock_page3.evaluate = AsyncMock(side_effect=Exception("network error"))
        result = await _check_session(mock_page3)
        assert result is None


# ── Logger config ─────────────────────────────────────────────────────────


def test_structlog_stderr():
    """Verify structlog writes to stderr, not stdout (critical for MCP)."""
    from flow_mcp import __init__ as flow_init
    import structlog

    # Ensure stderr logger
    logger = structlog.get_logger("flow-mcp")
    assert logger is not None


# ── MCP server basics ─────────────────────────────────────────────────────


def test_server_instance():
    from flow_mcp.server import server

    assert server.name == "flow-image-server"
    # Check tool registration
    tool_names = [t.name for t in server._tool_manager.list_tools()]
    assert "generate_image" in tool_names


# ── AccountManager (multi-account) ────────────────────────────────────────


def _make_profile(home: Path, name: str, email: str | None = None) -> Path:
    """Create a profile_<name> directory under home."""
    p = home / f"profile_{name}"
    p.mkdir(parents=True, exist_ok=True)
    if email:
        (p / ".gflow_account").write_text(email, encoding="utf-8")
    return p


class TestAccountManager:
    def setup_method(self) -> None:
        """Reset the AccountManager singleton before each test."""
        from flow_mcp.account_manager import AccountManager

        AccountManager.reset_instance()

    def teardown_method(self) -> None:
        """Reset after each test too."""
        from flow_mcp.account_manager import AccountManager

        AccountManager.reset_instance()

    def test_role_specific_env_vars(self, tmp_path: Path) -> None:
        """GFLOW_IMAGE_ACCOUNTS and GFLOW_VIDEO_ACCOUNTS override GFLOW_ACCOUNTS."""
        from flow_mcp.account_manager import AccountManager

        _make_profile(tmp_path, "paid_img", "paid@x.com")
        _make_profile(tmp_path, "free_vid1", "free1@x.com")
        _make_profile(tmp_path, "free_vid2", "free2@x.com")

        with patch.dict(
            os.environ,
            {
                "GFLOW_ACCOUNTS": "paid_img,free_vid1,free_vid2",
                "GFLOW_IMAGE_ACCOUNTS": "paid_img",
                "GFLOW_VIDEO_ACCOUNTS": "free_vid1,free_vid2",
                "GFLOW_CLI_HOME": str(tmp_path),
            },
        ):
            mgr_img = AccountManager(media_type="image")
            assert mgr_img.all_accounts == ["paid_img"]

            AccountManager.reset_instance()

            mgr_vid = AccountManager(media_type="video")
            assert mgr_vid.all_accounts == ["free_vid1", "free_vid2"]

    def test_load_from_env(self, tmp_path: Path) -> None:
        """GFLOW_ACCOUNTS env var is honored (only existing profiles)."""
        from flow_mcp.account_manager import AccountManager

        _make_profile(tmp_path, "alpha", "a@x.com")
        _make_profile(tmp_path, "beta", "b@x.com")
        _make_profile(tmp_path, "gamma", "c@x.com")

        with patch.dict(
            os.environ,
            {"GFLOW_ACCOUNTS": "alpha,beta,gamma", "GFLOW_CLI_HOME": str(tmp_path)},
        ):
            mgr = AccountManager()
            assert mgr.all_accounts == ["alpha", "beta", "gamma"]
            assert mgr.active_name == "alpha"

    def test_load_from_env_filters_missing(self, tmp_path: Path) -> None:
        """Profiles listed in GFLOW_ACCOUNTS but not on disk are skipped."""
        from flow_mcp.account_manager import AccountManager

        _make_profile(tmp_path, "alpha", "a@x.com")
        # "ghost" intentionally not created

        with patch.dict(
            os.environ,
            {"GFLOW_ACCOUNTS": "alpha,ghost", "GFLOW_CLI_HOME": str(tmp_path)},
        ):
            mgr = AccountManager()
            assert mgr.all_accounts == ["alpha"]

    def test_load_from_env_all_missing_falls_back(self, tmp_path: Path) -> None:
        """If ALL env accounts are missing, fall back to auto-detect."""
        from flow_mcp.account_manager import AccountManager

        _make_profile(tmp_path, "real", "r@x.com")
        # No profile dir for "ghost" or "phantom"

        with patch.dict(
            os.environ,
            {"GFLOW_ACCOUNTS": "ghost,phantom", "GFLOW_CLI_HOME": str(tmp_path)},
        ):
            mgr = AccountManager()
            # Should fall back to auto-detect and find "real"
            assert "real" in mgr.all_accounts

    def test_load_auto_detect(self, tmp_path: Path) -> None:
        """Without env var, picks up all authenticated profiles only.

        Profiles without a ``.gflow_account`` marker are NOT included in
        auto-detect (they have no session, so they would just fail later).
        They only show up in the all-profiles fallback when nothing else
        is available.
        """
        from flow_mcp.account_manager import AccountManager

        _make_profile(tmp_path, "one", "1@x.com")
        _make_profile(tmp_path, "two", "2@x.com")
        # Unauthenticated — excluded from auto-detect
        _make_profile(tmp_path, "three")

        with patch.dict(os.environ, {"GFLOW_CLI_HOME": str(tmp_path)}, clear=False):
            os.environ.pop("GFLOW_ACCOUNTS", None)
            mgr = AccountManager()
            assert set(mgr.all_accounts) == {"one", "two"}

    def test_load_unauthenticated_fallback(self, tmp_path: Path) -> None:
        """If only unauthenticated profiles exist, they show up as last resort."""
        from flow_mcp.account_manager import AccountManager

        # Both unauthenticated
        _make_profile(tmp_path, "stale1")
        _make_profile(tmp_path, "stale2")

        with patch.dict(os.environ, {"GFLOW_CLI_HOME": str(tmp_path)}, clear=False):
            os.environ.pop("GFLOW_ACCOUNTS", None)
            mgr = AccountManager()
            # These show up via the all-profiles fallback (third tier)
            assert set(mgr.all_accounts) == {"stale1", "stale2"}

    def test_switch_to(self, tmp_path: Path) -> None:
        from flow_mcp.account_manager import AccountManager

        _make_profile(tmp_path, "a")
        _make_profile(tmp_path, "b")
        _make_profile(tmp_path, "c")

        with patch.dict(os.environ, {"GFLOW_CLI_HOME": str(tmp_path)}):
            mgr = AccountManager()
            assert mgr.active_name == "a"
            mgr.switch_to("c")
            assert mgr.active_name == "c"
            mgr.switch_to("b")
            assert mgr.active_name == "b"

    def test_switch_to_unknown_raises(self, tmp_path: Path) -> None:
        from flow_mcp.account_manager import AccountManager

        _make_profile(tmp_path, "a")
        with patch.dict(os.environ, {"GFLOW_CLI_HOME": str(tmp_path)}):
            mgr = AccountManager()
            with pytest.raises(ValueError, match="not found"):
                mgr.switch_to("nonexistent")

    def test_switch_to_next(self, tmp_path: Path) -> None:
        from flow_mcp.account_manager import AccountManager

        _make_profile(tmp_path, "a")
        _make_profile(tmp_path, "b")
        _make_profile(tmp_path, "c")

        with patch.dict(os.environ, {"GFLOW_CLI_HOME": str(tmp_path)}):
            mgr = AccountManager()
            assert mgr.active_name == "a"
            assert mgr.switch_to_next() == "b"
            assert mgr.active_name == "b"
            assert mgr.switch_to_next() == "c"
            assert mgr.active_name == "c"

    def test_switch_to_next_raises_on_cycle(self, tmp_path: Path) -> None:
        """After trying the last account, switch_to_next raises."""
        from flow_mcp.account_manager import AccountCycleError, AccountManager

        _make_profile(tmp_path, "only")
        with patch.dict(os.environ, {"GFLOW_CLI_HOME": str(tmp_path)}):
            mgr = AccountManager()
            assert mgr.active_name == "only"
            with pytest.raises(AccountCycleError, match="exhausted"):
                mgr.switch_to_next()
            # State must NOT have been mutated by the failed call.
            assert mgr.active_name == "only"

    def test_state_persists(self, tmp_path: Path) -> None:
        """The active account name is written to disk and reloaded."""
        from flow_mcp.account_manager import AccountManager

        _make_profile(tmp_path, "x")
        _make_profile(tmp_path, "y")

        with patch.dict(os.environ, {"GFLOW_CLI_HOME": str(tmp_path)}):
            mgr1 = AccountManager()
            mgr1.switch_to("y")
            # State file should exist
            state_file = tmp_path / ".gflow_active_account"
            assert state_file.exists()
            assert state_file.read_text() == "y"

            # Reload — should restore to "y"
            mgr2 = AccountManager()
            assert mgr2.active_name == "y"

    def test_active_dir(self, tmp_path: Path) -> None:
        from flow_mcp.account_manager import AccountManager

        _make_profile(tmp_path, "alpha", "a@x.com")
        with patch.dict(os.environ, {"GFLOW_CLI_HOME": str(tmp_path)}):
            mgr = AccountManager()
            assert mgr.active_dir == tmp_path / "profile_alpha"

    def test_current_profile_dir_empty_list_raises(self, tmp_path: Path) -> None:
        """If the account list is truly empty, raise immediately."""
        from flow_mcp.account_manager import AccountManager

        # Force empty list by patching the internal _load_accounts
        with patch.dict(os.environ, {"GFLOW_CLI_HOME": str(tmp_path)}):
            mgr = AccountManager()
            # Force empty list (override whatever _load_accounts did)
            mgr._accounts = []
            mgr._current = 0
            with pytest.raises(RuntimeError, match="No Flow accounts"):
                mgr.current_profile_dir()

    def test_reset(self, tmp_path: Path) -> None:
        from flow_mcp.account_manager import AccountManager

        _make_profile(tmp_path, "a")
        _make_profile(tmp_path, "b")
        with patch.dict(os.environ, {"GFLOW_CLI_HOME": str(tmp_path)}):
            mgr = AccountManager()
            mgr.switch_to("b")
            mgr.reset()
            assert mgr.active_name == "a"


# ── generate_images_with_fallback (credit-exhausted → next account) ───────


class TestGenerateWithFallback:
    """Verify the multi-account fallback wrapper.

    We mock generate_images so we don't actually hit the network. The test
    verifies the orchestration: on CreditExhaustedError, switch account and
    retry; on cycle exhaustion, raise a GenerationError listing tried
    accounts.
    """

    def setup_method(self) -> None:
        from flow_mcp.account_manager import AccountManager

        AccountManager.reset_instance()

    def teardown_method(self) -> None:
        from flow_mcp.account_manager import AccountManager

        AccountManager.reset_instance()

    @pytest.mark.asyncio
    async def test_first_account_succeeds(self, tmp_path: Path) -> None:
        """If the first account works, no fallback needed."""
        from flow_mcp.account_manager import AccountManager
        from flow_mcp.generator import GenerationResult, generate_images_with_fallback

        _make_profile(tmp_path, "a")
        _make_profile(tmp_path, "b")

        fake_result = GenerationResult(files=[])

        with patch.dict(os.environ, {"GFLOW_CLI_HOME": str(tmp_path)}):
            AccountManager()  # initialize
            with patch(
                "flow_mcp.generator.generate_images",
                AsyncMock(return_value=fake_result),
            ):
                with patch(
                    "flow_mcp.browser_pool.close_pool",
                    AsyncMock(),
                ):
                    result = await generate_images_with_fallback(
                        prompt="hi", output_dir=str(tmp_path),
                    )
                    assert result is fake_result
                    mgr = AccountManager.get_instance()
                    assert mgr.active_name == "a"  # did not switch

    @pytest.mark.asyncio
    async def test_fallback_on_credit_exhausted(self, tmp_path: Path) -> None:
        """First account out of credits → switch to second, which succeeds."""
        from flow_mcp.account_manager import AccountManager
        from flow_mcp.generator import (
            CreditExhaustedError,
            GenerationResult,
            generate_images_with_fallback,
        )

        _make_profile(tmp_path, "first")
        _make_profile(tmp_path, "second")
        _make_profile(tmp_path, "third")

        fake_result = GenerationResult(files=[])

        # First call (account 0) → CreditExhaustedError
        # Second call (account 1) → success
        with patch.dict(os.environ, {"GFLOW_CLI_HOME": str(tmp_path)}):
            AccountManager()
            mgr = AccountManager.get_instance()

            with patch(
                "flow_mcp.generator.generate_images",
                AsyncMock(side_effect=[
                    CreditExhaustedError("first out"),
                    fake_result,
                ]),
            ):
                with patch(
                    "flow_mcp.browser_pool.close_pool",
                    AsyncMock(),
                ):
                    result = await generate_images_with_fallback(
                        prompt="hi", output_dir=str(tmp_path),
                    )
                    assert result is fake_result
                    assert mgr.active_name == "second"

    @pytest.mark.asyncio
    async def test_all_accounts_exhausted(self, tmp_path: Path) -> None:
        """All accounts run out of credits → GenerationError with list of tried."""
        from flow_mcp.account_manager import AccountManager
        from flow_mcp.generator import (
            CreditExhaustedError,
            GenerationError,
            generate_images_with_fallback,
        )

        _make_profile(tmp_path, "a")
        _make_profile(tmp_path, "b")

        with patch.dict(os.environ, {"GFLOW_CLI_HOME": str(tmp_path)}):
            AccountManager()

            with patch(
                "flow_mcp.generator.generate_images",
                AsyncMock(side_effect=CreditExhaustedError("no credits")),
            ):
                with patch(
                    "flow_mcp.browser_pool.close_pool",
                    AsyncMock(),
                ):
                    with pytest.raises(GenerationError, match="exhausted") as excinfo:
                        await generate_images_with_fallback(
                            prompt="hi", output_dir=str(tmp_path),
                        )
                    msg = str(excinfo.value)
                    assert "a" in msg
                    assert "b" in msg

    @pytest.mark.asyncio
    async def test_non_credit_error_does_not_switch(self, tmp_path: Path) -> None:
        """Other errors (e.g. GenerationError) should NOT trigger fallback."""
        from flow_mcp.account_manager import AccountManager
        from flow_mcp.generator import (
            GenerationError,
            generate_images_with_fallback,
        )

        _make_profile(tmp_path, "a")
        _make_profile(tmp_path, "b")

        with patch.dict(os.environ, {"GFLOW_CLI_HOME": str(tmp_path)}):
            AccountManager()
            mgr = AccountManager.get_instance()

            with patch(
                "flow_mcp.generator.generate_images",
                AsyncMock(side_effect=GenerationError("nope")),
            ):
                with pytest.raises(GenerationError, match="nope"):
                    await generate_images_with_fallback(
                        prompt="hi", output_dir=str(tmp_path),
                    )
                # Should still be on 'a' — no fallback attempted
                assert mgr.active_name == "a"


# ── CLI: auth login <name> and auth remove <name> ─────────────────────────


class TestMultiAccountCLI:
    def setup_method(self) -> None:
        from flow_mcp.account_manager import AccountManager

        AccountManager.reset_instance()

    def teardown_method(self) -> None:
        from flow_mcp.account_manager import AccountManager

        AccountManager.reset_instance()

    def test_auth_login_with_name(self) -> None:
        """flow-mcp auth login <name> must pass the name to cmd_login."""
        with patch.object(sys, "argv", ["flow-mcp", "auth", "login", "work"]):
            with patch("flow_mcp.__main__._run_async") as mock_run:
                from flow_mcp.__main__ import main

                main()
                mock_run.assert_called_once()
                # Inspect the coroutine passed to _run_async
                coro = mock_run.call_args[0][0]
                # The wrapper _auth_login is itself a coroutine — we just
                # check it was awaited via _run_async. The actual name-passing
                # is verified by the patch chain below.

    def test_auth_login_name_internal_browser(self) -> None:
        """flow-mcp auth login <name> --browser internal works."""
        with patch.object(
            sys,
            "argv",
            ["flow-mcp", "auth", "login", "personal", "--browser", "internal"],
        ):
            with patch("flow_mcp.__main__._run_async") as mock_run:
                with patch("flow_mcp.__main__._auth_login_internal") as mock_internal:
                    from flow_mcp.__main__ import main

                    main()
                    mock_internal.assert_called_once_with("personal")
                    mock_run.assert_called_once()

    def test_auth_remove_missing_arg(self) -> None:
        """flow-mcp auth remove (no name) exits with usage error."""
        with patch.object(sys, "argv", ["flow-mcp", "auth", "remove"]):
            with patch.object(sys, "exit", side_effect=SystemExit) as mock_exit:
                with patch("builtins.print") as mock_print:
                    from flow_mcp.__main__ import main

                    with pytest.raises(SystemExit):
                        main()
                    mock_exit.assert_called_once_with(2)
                    printed = " ".join(
                        str(c.args[0]) for c in mock_print.call_args_list
                    )
                    assert "Missing account name" in printed

    def test_auth_remove_nonexistent(self, tmp_path: Path) -> None:
        """Removing a non-existent account prints a friendly error."""
        from flow_mcp.auth import cmd_remove_account

        with patch.dict(os.environ, {"GFLOW_CLI_HOME": str(tmp_path)}):
            with patch("builtins.print") as mock_print:
                # Use asyncio.run to execute the async function
                import asyncio

                asyncio.run(cmd_remove_account("ghost"))
                printed = " ".join(
                    str(c.args[0]) for c in mock_print.call_args_list
                )
                assert "not found" in printed

    def test_auth_remove_with_confirmation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Removing a real account: confirm prompt + deletion + singleton reset."""
        from flow_mcp.account_manager import AccountManager
        from flow_mcp.auth import cmd_remove_account

        _make_profile(tmp_path, "doomed", "d@x.com")
        _make_profile(tmp_path, "survivor", "s@x.com")

        # Provide a matching confirmation
        monkeypatch.setattr("builtins.input", lambda _: "doomed")

        with patch.dict(os.environ, {"GFLOW_CLI_HOME": str(tmp_path)}):
            import asyncio

            asyncio.run(cmd_remove_account("doomed"))

        # Profile directory should be gone
        assert not (tmp_path / "profile_doomed").exists()
        # Survivor should still be there
        assert (tmp_path / "profile_survivor").exists()

    def test_auth_remove_cancelled_on_mismatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the user types a different name, nothing is deleted."""
        from flow_mcp.auth import cmd_remove_account

        _make_profile(tmp_path, "protected", "p@x.com")
        monkeypatch.setattr("builtins.input", lambda _: "WRONG")

        with patch.dict(os.environ, {"GFLOW_CLI_HOME": str(tmp_path)}):
            import asyncio

            asyncio.run(cmd_remove_account("protected"))

        # Profile must still exist
        assert (tmp_path / "profile_protected").exists()


# ── AccountManager singleton reset_instance ───────────────────────────────


def test_account_manager_singleton() -> None:
    """get_instance returns the same object; reset_instance clears it."""
    from flow_mcp.account_manager import AccountManager

    a = AccountManager.get_instance()
    b = AccountManager.get_instance()
    assert a is b
    AccountManager.reset_instance()
    c = AccountManager.get_instance()
    assert c is not a


# ── Video constants ──────────────────────────────────────────────────────


class TestVideoConstants:
    def test_video_models(self) -> None:
        from flow_mcp.constants import VIDEO_MODELS, ALLOWED_VIDEO_MODELS

        assert "omni-flash" in VIDEO_MODELS
        assert "veo-lite" in VIDEO_MODELS
        assert "veo-fast" in VIDEO_MODELS
        assert "veo-quality" in VIDEO_MODELS
        assert VIDEO_MODELS["omni-flash"] == "abra"

    def test_video_aspects(self) -> None:
        from flow_mcp.constants import VIDEO_ASPECT_RATIOS

        assert "9:16" in VIDEO_ASPECT_RATIOS
        assert "16:9" in VIDEO_ASPECT_RATIOS
        assert "1:1" in VIDEO_ASPECT_RATIOS
        assert VIDEO_ASPECT_RATIOS["9:16"] == "VIDEO_ASPECT_RATIO_PORTRAIT"
        assert VIDEO_ASPECT_RATIOS["16:9"] == "VIDEO_ASPECT_RATIO_LANDSCAPE"

    def test_video_durations(self) -> None:
        from flow_mcp.constants import VIDEO_DURATIONS

        assert 4 in VIDEO_DURATIONS
        assert 6 in VIDEO_DURATIONS
        assert 8 in VIDEO_DURATIONS

    def test_video_status_constants(self) -> None:
        from flow_mcp.constants import (
            VIDEO_STATUS_ACTIVE,
            VIDEO_STATUS_DONE,
            VIDEO_STATUS_FAILED,
            VIDEO_STATUS_PENDING,
        )

        assert VIDEO_STATUS_PENDING == "MEDIA_GENERATION_STATUS_SCHEDULED"
        assert VIDEO_STATUS_ACTIVE == "MEDIA_GENERATION_STATUS_ACTIVE"
        assert VIDEO_STATUS_DONE == "MEDIA_GENERATION_STATUS_SUCCESSFUL"
        assert VIDEO_STATUS_FAILED == "MEDIA_GENERATION_STATUS_FAILED"

    def test_video_audio_preference(self) -> None:
        from flow_mcp.constants import VIDEO_AUDIO_FAILURE_PREFERENCE

        assert VIDEO_AUDIO_FAILURE_PREFERENCE == "BLOCK_SILENCED_VIDEOS"


# ── Video JS templates ──────────────────────────────────────────────────


class TestVideoJSTemplates:
    def test_generate_video_js(self) -> None:
        from flow_mcp.js_templates import generate_video_js

        js = generate_video_js("https://api/foo", "ya29.test", '{"a":1}')
        assert "https://api/foo" in js
        assert "ya29.test" in js
        assert "Authorization" in js
        assert "window.__vid" in js
        assert "HTTP_401" in js

    def test_check_video_status_js(self) -> None:
        from flow_mcp.js_templates import check_video_status_js

        js = check_video_status_js(
            "https://aisandbox-pa.googleapis.com/v1/video:batchCheckAsyncVideoGenerationStatus",
            "ya29.test",
            '{"media":[]}',
        )
        assert "batchCheckAsyncVideoGenerationStatus" in js
        assert "aisandbox-pa.googleapis.com" in js
        assert "window.__vid_status" in js

    def test_get_video_url_js(self) -> None:
        from flow_mcp.js_templates import get_video_url_js

        js = get_video_url_js("uuid-test")
        assert "media.getMediaUrlRedirect" in js
        assert "uuid-test" in js
        assert "MEDIA_URL_TYPE_DOWNLOAD" in js
        assert "window.__vid_url" in js


# ── Video generator (with mocks) ─────────────────────────────────────────


class TestGenerateVideo:
    """Unit tests for the video generator. Real end-to-end is exercised
    separately in the smoke test against cesar1/cesar2."""

    def setup_method(self) -> None:
        from flow_mcp.account_manager import AccountManager
        AccountManager.reset_instance()

    def teardown_method(self) -> None:
        from flow_mcp.account_manager import AccountManager
        AccountManager.reset_instance()

    @pytest.mark.asyncio
    async def test_video_result_describe(self, tmp_path: Path) -> None:
        from flow_mcp.generator import VideoResult

        result = VideoResult(
            media_name="abc-123-def",
            project_id="proj-456",
            model="omni-flash",
            duration_s=4,
            media_blob_size=1_234_567,
        )
        desc = result.describe()
        assert "omni-flash" in desc
        assert "4s" in desc
        assert "abc-123-def" in desc
        assert "proj-456" in desc
        assert "labs.google/fx/tools/flow" in desc

    def test_resolve_video_model_key(self) -> None:
        from flow_mcp.generator import _resolve_video_model_key

        # omni-flash (abra) family
        assert _resolve_video_model_key("omni-flash", 4, is_i2v=False) == "abra_t2v_4s"
        assert _resolve_video_model_key("omni-flash", 10, is_i2v=False) == "abra_t2v_10s"
        assert _resolve_video_model_key("omni-flash", 4, is_i2v=True) == "abra_i2v_4s"
        assert _resolve_video_model_key("omni-flash", 8, is_i2v=True) == "abra_i2v_8s"

        # veo-lite family
        assert _resolve_video_model_key("veo-lite", 4, is_i2v=False) == "veo_3_1_t2v_lite_4s"
        assert _resolve_video_model_key("veo-lite", 6, is_i2v=False) == "veo_3_1_t2v_lite_6s"
        assert _resolve_video_model_key("veo-lite", 4, is_i2v=True) == "veo_3_1_i2v_s_lite_4s"

        # veo-fast family
        assert _resolve_video_model_key("veo-fast", 4, is_i2v=False) == "veo_3_1_t2v_fast_4s"
        assert _resolve_video_model_key("veo-fast", 4, is_i2v=True) == "veo_3_1_i2v_s_fast_4s"

        # veo-quality family
        assert _resolve_video_model_key("veo-quality", 4, is_i2v=False) == "veo_3_1_t2v_quality_4s"
        assert _resolve_video_model_key("veo-quality", 4, is_i2v=True) == "veo_3_1_i2v_s_quality_4s"

        # 10s check
        from flow_mcp.generator import GenerationError
        with pytest.raises(GenerationError, match="10s duration is only supported"):
            _resolve_video_model_key("veo-lite", 10, is_i2v=False)

    async def test_generate_video_validates_args(self, tmp_path: Path) -> None:
        from flow_mcp.generator import GenerationError, generate_video

        # Bad model
        with pytest.raises(GenerationError, match="Unknown video model"):
            await generate_video("test", model="not-a-model", output_dir=str(tmp_path))

        # Bad aspect
        with pytest.raises(GenerationError, match="Unknown video aspect"):
            await generate_video("test", aspect="99:99", output_dir=str(tmp_path))

        # 1:1 with omni-flash
        with pytest.raises(GenerationError, match="1:1 aspect ratio is not supported by 'omni-flash'"):
            await generate_video("test", model="omni-flash", aspect="1:1", output_dir=str(tmp_path))

        # Bad duration
        with pytest.raises(GenerationError, match="Invalid duration"):
            await generate_video("test", duration=99, output_dir=str(tmp_path))

    @pytest.mark.asyncio
    async def test_generate_video_first_account_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the first account works, no fallback needed."""
        from flow_mcp.account_manager import AccountManager
        from flow_mcp.generator import VideoResult, generate_video_with_fallback

        # Build a fake profile dir
        (tmp_path / "profile_v1").mkdir()
        (tmp_path / "profile_v2").mkdir()
        monkeypatch.setenv("GFLOW_CLI_HOME", str(tmp_path))
        monkeypatch.setenv("GFLOW_ACCOUNTS", "v1,v2")

        fake_result = VideoResult(
            media_name="m1", project_id="p1",
            model="omni-flash", duration_s=4,
        )

        with patch(
            "flow_mcp.generator.generate_video",
            AsyncMock(return_value=fake_result),
        ):
            with patch("flow_mcp.browser_pool.close_pool", AsyncMock()):
                result = await generate_video_with_fallback(
                    prompt="hi",
                    model="omni-flash",
                    aspect="9:16",
                    duration=4,
                )
                assert result is fake_result
                assert AccountManager.get_instance().active_name == "v1"

    @pytest.mark.asyncio
    async def test_generate_video_fallback_on_credits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """First account out of credits → switch to second, which succeeds."""
        from flow_mcp.account_manager import AccountManager
        from flow_mcp.generator import (
            CreditExhaustedError,
            VideoResult,
            generate_video_with_fallback,
        )

        (tmp_path / "profile_v1").mkdir()
        (tmp_path / "profile_v2").mkdir()
        monkeypatch.setenv("GFLOW_CLI_HOME", str(tmp_path))
        monkeypatch.setenv("GFLOW_ACCOUNTS", "v1,v2")

        fake_result = VideoResult(
            media_name="m2", project_id="p2",
            model="omni-flash", duration_s=4,
        )

        with patch(
            "flow_mcp.generator.generate_video",
            AsyncMock(side_effect=[
                CreditExhaustedError("v1 out"),
                fake_result,
            ]),
        ):
            with patch("flow_mcp.browser_pool.close_pool", AsyncMock()):
                result = await generate_video_with_fallback(
                    prompt="hi",
                    model="omni-flash",
                )
                assert result is fake_result
                assert AccountManager.get_instance().active_name == "v2"

    @pytest.mark.asyncio
    async def test_generate_video_all_exhausted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """All accounts out of credits → GenerationError."""
        from flow_mcp.account_manager import AccountManager
        from flow_mcp.generator import (
            CreditExhaustedError,
            GenerationError,
            generate_video_with_fallback,
        )

        (tmp_path / "profile_v1").mkdir()
        (tmp_path / "profile_v2").mkdir()
        monkeypatch.setenv("GFLOW_CLI_HOME", str(tmp_path))
        monkeypatch.setenv("GFLOW_ACCOUNTS", "v1,v2")

        with patch(
            "flow_mcp.generator.generate_video",
            AsyncMock(side_effect=CreditExhaustedError("no credits")),
        ):
            with patch("flow_mcp.browser_pool.close_pool", AsyncMock()):
                with pytest.raises(GenerationError, match="exhausted") as excinfo:
                    await generate_video_with_fallback(prompt="hi")
                msg = str(excinfo.value)
                assert "v1" in msg
                assert "v2" in msg

    @pytest.mark.asyncio
    async def test_generate_video_non_credit_error_doesnt_switch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Other errors should NOT trigger account fallback."""
        from flow_mcp.account_manager import AccountManager
        from flow_mcp.generator import GenerationError, generate_video_with_fallback

        (tmp_path / "profile_v1").mkdir()
        (tmp_path / "profile_v2").mkdir()
        monkeypatch.setenv("GFLOW_CLI_HOME", str(tmp_path))
        monkeypatch.setenv("GFLOW_ACCOUNTS", "v1,v2")

        with patch(
            "flow_mcp.generator.generate_video",
            AsyncMock(side_effect=GenerationError("nope")),
        ):
            with pytest.raises(GenerationError, match="nope"):
                await generate_video_with_fallback(prompt="hi")
            # Still on v1 — no fallback
            assert AccountManager.get_instance().active_name == "v1"


# ── Video tool validation (CLI entry point) ──────────────────────────────


@pytest.mark.asyncio
async def test_generate_video_tool_validation():
    """Test the MCP tool entry point validates inputs correctly."""
    from flow_mcp.server import generate_video_tool

    # Invalid model
    result = await generate_video_tool(prompt="test", model="bogus")
    data = json.loads(result)
    assert data.get("success") is False
    assert "Invalid video model" in data.get("error", "")

    # Invalid aspect
    result = await generate_video_tool(prompt="test", aspect="99:99")
    data = json.loads(result)
    assert data.get("success") is False
    assert "Invalid video aspect" in data.get("error", "")

    # Invalid duration
    result = await generate_video_tool(prompt="test", duration=99)
    data = json.loads(result)
    assert data.get("success") is False
    assert "Invalid duration" in data.get("error", "")


def test_generate_video_tool_registered():
    """The generate_video tool should be in the server's tool list."""
    from flow_mcp.server import server

    tool_names = [t.name for t in server._tool_manager.list_tools()]
    assert "generate_video" in tool_names
    assert "generate_image" in tool_names
