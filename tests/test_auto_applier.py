import pytest
from unittest.mock import AsyncMock

from src.applier.auto_applier import HHAutoApplier
from src.db.models import ApplicationStatus


@pytest.mark.asyncio
async def test_apply_already_applied():
    applier = HHAutoApplier()
    
    mock_page = AsyncMock()
    mock_already_elem = AsyncMock()
    mock_already_elem.is_visible.return_value = True

    # Page.query_selector returns already applied element
    mock_page.query_selector.side_effect = lambda sel: mock_already_elem if sel in applier.ALREADY_APPLIED_SELECTORS else None

    result = await applier.apply_to_vacancy(mock_page, vacancy_url="https://hh.ru/vacancy/100", external_id="100")
    assert result.status == ApplicationStatus.ALREADY_APPLIED


@pytest.mark.asyncio
async def test_apply_skips_questions():
    applier = HHAutoApplier()

    mock_page = AsyncMock()
    mock_apply_btn = AsyncMock()
    mock_apply_btn.is_visible.return_value = True
    mock_apply_btn.is_enabled.return_value = True

    mock_question_elem = AsyncMock()
    mock_question_elem.is_visible.return_value = True

    def query_selector_side_effect(sel):
        if sel in applier.APPLY_BUTTON_SELECTORS:
            return mock_apply_btn
        if sel in applier.QUESTION_CONTAINER_SELECTORS:
            return mock_question_elem
        return None

    mock_page.query_selector.side_effect = query_selector_side_effect
    mock_page.query_selector_all.return_value = []

    result = await applier.apply_to_vacancy(mock_page, vacancy_url="https://hh.ru/vacancy/200", external_id="200")
    assert result.status == ApplicationStatus.SKIPPED_QUESTIONS
