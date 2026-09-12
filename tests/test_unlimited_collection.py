"""Exercise real pagination without making requests to hh.ru."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

from src.collector import collector as module
from src.collector.collector import HHVacancyCardCollector
from src.config import BrowserConfig, ScrollerConfig, Config
from src.db.models import PageCommitParams, CollectionSummary, VacancyCard


@pytest.fixture
def search(monkeypatch):
    page = SimpleNamespace(url='', number=1, count=13)
    async def goto(url, **kwargs):
        page.url = url
        page.number = int(parse_qs(urlsplit(url).query).get('page', ['1'])[0])
    async def click():
        page.number += 1
        page.url = f'https://hh.ru/search/vacancy?page={page.number}'
    button = SimpleNamespace(is_visible=AsyncMock(return_value=True),
                             is_enabled=AsyncMock(return_value=True), click=AsyncMock(side_effect=click))
    async def query(selector):
        return button if page.number < page.count else None
    page.goto = AsyncMock(side_effect=goto)
    page.query_selector = AsyncMock(side_effect=query)
    page.wait_for_function = AsyncMock()
    page.evaluate = AsyncMock()
    @asynccontextmanager
    async def connect(self):
        yield page
    monkeypatch.setattr(module.BrowserConnector, 'connect', connect)
    monkeypatch.setattr(module, 'asyncio', SimpleNamespace(sleep=AsyncMock()))
    async def parse(page, **kwargs):
        return [(VacancyCard(external_id=str(page.number)), None)]
    collector = HHVacancyCardCollector(
        card_parser=SimpleNamespace(parse_cards_from_page=AsyncMock(side_effect=parse)),
        page_guard=SimpleNamespace(check_page_state=AsyncMock()),
        popup_handler=SimpleNamespace(setup_dialog_handler=Mock(), dismiss_known_overlays=AsyncMock()),
    )
    collector._wait_for_cards = AsyncMock(return_value=1)
    return collector, page, button


async def run(search, limiter=None, stop_after=None, on_retry=None):
    collector, page, button = search
    results = []
    async for item in collector.collect(BrowserConfig(), 'https://hh.ru/search/vacancy', 'test',
                                        scroller_config=ScrollerConfig(load_mode='instant'), limiter=limiter,
                                        on_retry=on_retry):
        results.append(item)
        if isinstance(item, PageCommitParams) and item.page_number == stop_after:
            collector.stop()
    return results


async def test_collect_passes_tenth_page_and_finishes_at_end(search):
    limiter = SimpleNamespace(acquire=AsyncMock())
    results = await run(search, limiter)
    pages = [item for item in results if isinstance(item, PageCommitParams)]
    assert [p.page_number for p in pages] == list(range(1, 14))
    assert results[-1].completion_reason == 'no_more_pages'
    assert results[-1].total_pages_processed == 13
    assert search[2].click.await_count == 12
    assert limiter.acquire.await_count >= 13


async def test_user_stop_keeps_last_page_and_does_not_navigate(search):
    results = await run(search, stop_after=2)
    assert results[-1].completion_reason == 'stopped_by_user'
    assert results[-1].total_pages_processed == 2
    assert search[2].click.await_count == 1


async def test_stop_during_pacing_does_not_open_next_page(search):
    calls = 0
    async def acquire(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            search[0].stop()
    results = await run(search, SimpleNamespace(acquire=AsyncMock(side_effect=acquire)))
    assert results[-1].completion_reason == 'stopped_by_user'
    search[2].click.assert_not_awaited()


async def test_pagination_loop_stops_without_reprocessing(search):
    search[2].click.side_effect = None
    with pytest.raises(RuntimeError, match='уже обработанную страницу'):
        await run(search)
    assert search[0].card_parser.parse_cards_from_page.await_count == 1


async def test_crashed_search_page_retries_instead_of_committing_zero_cards(search):
    collector, page, _ = search
    page.count = 2
    collector._wait_for_cards.side_effect = [PlaywrightError('Target crashed'), 1, 1]
    reports = []
    results = await run(search, on_retry=reports.append)
    pages = [item for item in results if isinstance(item, PageCommitParams)]
    assert [(p.page_number, [c.external_id for c in p.cards]) for p in pages] == [(1, ['1']), (2, ['2'])]
    assert results[-1].unique_vacancies == 2
    assert results[-1].completion_reason == 'no_more_pages'
    assert page.goto.await_count == 2
    assert len(reports) == 1 and 'восстанавливаю' in reports[0]


async def test_crash_after_commit_does_not_repeat_page_or_lose_checkpoint(search):
    collector, page, button = search
    page.count = 2
    query = page.query_selector.side_effect
    crashed = False
    async def once(selector):
        nonlocal crashed
        if not crashed:
            crashed = True
            raise PlaywrightError('Target page, context or browser has been closed')
        return await query(selector)
    page.query_selector.side_effect = once
    results = await run(search)
    pages = [item for item in results if isinstance(item, PageCommitParams)]
    assert [p.page_number for p in pages] == [1, 2]
    assert results[-1].total_pages_processed == 2
    assert results[-1].unique_vacancies == 2
    assert collector.card_parser.parse_cards_from_page.await_count == 2
    assert page.goto.await_count == 2
    assert button.click.await_count == 1


async def test_next_page_navigation_timeout_retries_from_last_committed_page(search):
    _, page, _ = search
    page.count = 2
    page.wait_for_function.side_effect = [PlaywrightTimeoutError('navigation timeout'), None, None]
    results = await run(search)
    assert [item.page_number for item in results if isinstance(item, PageCommitParams)] == [1, 2]
    assert page.goto.await_args_list[1].args[0] == 'https://hh.ru/search/vacancy'
    assert results[-1].unique_vacancies == 2


async def test_changed_url_with_stale_cards_retries_before_committing_next_page(search):
    collector, page, _ = search
    page.count = 2
    page.wait_for_function.side_effect = [None, PlaywrightTimeoutError('old result cards still visible'), None, None]
    results = await run(search)
    assert [item.page_number for item in results if isinstance(item, PageCommitParams)] == [1, 2]
    assert page.goto.await_args_list[1].args[0] == 'https://hh.ru/search/vacancy'
    assert collector.card_parser.parse_cards_from_page.await_count == 2


async def test_stop_while_browser_is_unavailable_does_not_report_success(search):
    collector, page, _ = search
    collector._wait_for_cards.side_effect = PlaywrightError('Target crashed')
    results = await run(search, on_retry=lambda _: collector.stop())
    assert not any(isinstance(item, PageCommitParams) for item in results)
    assert results[-1].total_pages_processed == 0
    assert results[-1].final_status == 'cancelled'
    assert results[-1].completion_reason == 'stopped_by_user'
    assert page.goto.await_count == 1


async def test_unknown_parser_error_is_not_retried_or_reported_as_empty(search):
    collector, _, _ = search
    collector.card_parser.parse_cards_from_page.side_effect = ValueError('broken parser')
    with pytest.raises(ValueError, match='broken parser'):
        await run(search)


async def test_persistent_search_failure_ends_after_three_attempts(search):
    collector, page, _ = search
    collector._wait_for_cards.side_effect = PlaywrightTimeoutError('HH never loads')
    with pytest.raises(RuntimeError, match='3 попытки'):
        await run(search)
    assert page.goto.await_count == 3
    collector.card_parser.parse_cards_from_page.assert_not_awaited()


async def test_persistent_pager_failure_does_not_recommit_saved_cards(search):
    collector, page, _ = search
    page.wait_for_function.side_effect = PlaywrightTimeoutError('HH never changes page')
    committed = []
    with pytest.raises(RuntimeError, match='Уже собранные вакансии сохранены'):
        async for item in collector.collect(BrowserConfig(), 'https://hh.ru/search/vacancy', 'test',
                                           scroller_config=ScrollerConfig(load_mode='instant')):
            if isinstance(item, PageCommitParams):
                committed.append(item)
    assert len(committed) == 1
    assert page.goto.await_count == 3
    assert collector.card_parser.parse_cards_from_page.await_count == 1


async def test_explicit_empty_results_can_complete(search):
    collector, _, button = search
    collector._wait_for_cards.return_value = 0
    results = await run(search)
    assert results[-1].completion_reason == 'no_results'
    assert results[-1].total_cards_found == 0
    collector.card_parser.parse_cards_from_page.assert_not_awaited()
    button.click.assert_not_awaited()


async def test_selector_timeout_does_not_mean_no_results():
    collector = HHVacancyCardCollector(page_guard=SimpleNamespace(check_page_state=AsyncMock()))
    page = SimpleNamespace(wait_for_selector=AsyncMock(side_effect=PlaywrightTimeoutError('not ready')))
    with pytest.raises(module.SearchPageNotReady):
        await collector._wait_for_cards(page, 1)


async def test_crash_while_waiting_for_cards_propagates():
    collector = HHVacancyCardCollector()
    page = SimpleNamespace(wait_for_selector=AsyncMock(side_effect=PlaywrightError('Target crashed')))
    with pytest.raises(PlaywrightError, match='Target crashed'):
        await collector._wait_for_cards(page, 1)


async def test_crashed_pager_query_is_not_the_last_page():
    page = SimpleNamespace(query_selector=AsyncMock(side_effect=PlaywrightError('Target crashed')))
    with pytest.raises(PlaywrightError, match='Target crashed'):
        await HHVacancyCardCollector()._go_to_next_page(page)


async def test_crashed_card_query_is_not_an_empty_list():
    from src.collector.card_parser import VacancyCardParser
    page = SimpleNamespace(query_selector_all=AsyncMock(side_effect=PlaywrightError('Target crashed')))
    with pytest.raises(PlaywrightError, match='Target crashed'):
        await VacancyCardParser().parse_cards_from_page(page)


async def test_scroll_retry_keeps_cards_read_before_the_crash(search):
    collector, page, _ = search
    page.count = 1
    card = VacancyCard(external_id='42')
    collector.card_parser.parse_cards_from_page.side_effect = [
        [(card, None)], PlaywrightError('Target crashed'), [(card, None)], [(card, None)], [(card, None)],
    ]
    async def scroll(page, on_step_callback):
        await on_step_callback()
        await on_step_callback()
    collector.build_scroll_engine = Mock(return_value=SimpleNamespace(scroll_page=scroll, stop=Mock()))
    results = [item async for item in collector.collect(
        BrowserConfig(), 'https://hh.ru/search/vacancy', 'test', scroller_config=ScrollerConfig(load_mode='scroll'),
    )]
    pages = [item for item in results if isinstance(item, PageCommitParams)]
    assert len(pages) == 1 and [c.external_id for c in pages[0].cards] == ['42']
    assert results[-1].unique_vacancies == 1


async def test_guard_checks_redirect_after_next_page(search):
    from src.browser.intervention import HHInterventionRequired
    async def guard(page, **kwargs):
        if page.number == 2:
            raise HHInterventionRequired('captcha')
    search[0].page_guard.check_page_state.side_effect = guard
    with pytest.raises(HHInterventionRequired):
        await run(search)
    assert search[0].card_parser.parse_cards_from_page.await_count == 1


def test_old_yaml_page_limit_does_not_limit_config(tmp_path):
    config_file = tmp_path / 'legacy.yaml'
    config_file.write_text('hh_autoscroller:\n  max_pages: 1\n', encoding='utf-8')
    assert not hasattr(Config.load(str(config_file)).scroller, 'max_pages')
