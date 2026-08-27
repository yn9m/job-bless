import os
import pytest
from unittest.mock import patch, AsyncMock


@pytest.mark.asyncio
async def test_manual_login_session_file():
    from src.browser.login_session import run_manual_login
    
    with patch("src.browser.login_session.LocalProcessLauncher") as MockLauncher, \
         patch("src.browser.login_session.async_playwright") as MockPlaywright, \
         patch("asyncio.get_event_loop") as MockLoop:
        
        mock_launcher = MockLauncher.return_value
        mock_launcher.start.return_value = ("http://localhost:9222", 1234)

        mock_browser = AsyncMock()
        mock_context = AsyncMock()
        mock_page = AsyncMock()
        mock_context.new_page.return_value = mock_page
        mock_context.storage_state.return_value = None
        mock_browser.contexts = [mock_context]
        
        mock_pw = AsyncMock()
        mock_pw.chromium.connect_over_cdp.return_value = mock_browser

        mock_pw_manager = AsyncMock()
        mock_pw_manager.__aenter__.return_value = mock_pw
        MockPlaywright.return_value = mock_pw_manager

        mock_loop = AsyncMock()
        mock_loop.run_in_executor.return_value = None
        MockLoop.return_value = mock_loop

        # Run helper with dry run / mocks
        storage_path = await run_manual_login("configs/config.local.yaml")
        assert storage_path.endswith("storage_state.json")
