"""Background jobs triggered from the web UI and by the scheduler."""

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


from src.activity.service import ActivityScroller
from src.applier.auto_applier import HHAutoApplier
from src.applier.cover_letter import build_writer
from src.browser.account import HH_COOKIE_DOMAIN, LOGIN_SELECTORS, is_hh_url, read_hh_account
from src.browser.connector import LIVE_PAGES, BrowserConnector
from src.browser.session import SESSION, save_storage_state as _save_hh_session
from src.browser.intervention import HHInterventionRequired, intervention_message
from src.collector.collector import HHVacancyCardCollector
from src.collector.detail_parser import VacancyDetailsReader
from src.config import BrowserConfig
from src.db.models import (
    ApplicationStatus,
    CollectionSummary,
    PageCommitParams,
    SearchRun,
    SearchRunStatus,
    VacancyDetails,
)
from src.llm import create_llm_client
from src.matching.scorer import VacancyScorer
from src.resume.profile import ProfileBuilder
from src.resume.service import ResumeService
from src.resume.toucher import ResumeToucher
from src.web.tasks import TaskContext

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# browser helpers
# ----------------------------------------------------------------------

async def close_stream(stream) -> None:  # noqa: ANN001
    """Finalize an async generator so its browser tab is released now.

    An abandoned generator is only closed by the garbage collector, which can
    leave a browser tab hanging around for minutes after «Стоп». When the job
    itself is being cancelled, the close is shielded so it still completes.
    """
    closing = asyncio.ensure_future(stream.aclose())
    try:
        await asyncio.shield(closing)
    except asyncio.CancelledError:
        # We are cancelled, the shielded close keeps running and closes the tab.
        pass
    except Exception as e:  # noqa: BLE001
        logger.warning("could not close the collector stream: %s", e)


def _log_rate_limits(ctx: TaskContext) -> None:
    limits = ctx.limiter.config
    if not limits.enabled:
        ctx.log("ограничение нагрузки выключено")
        return
    ctx.log(
        f"темп: не чаще одной страницы в {limits.min_interval_sec:.1f} с "
        f"(+до {limits.jitter_sec:.1f} с), потолок {limits.requests_per_minute} запросов/мин"
    )


async def ensure_browser(ctx: TaskContext) -> BrowserConfig:
    """Docker startup is owned by the application, not individual jobs."""
    return ctx.settings.browser_config()


# ----------------------------------------------------------------------
# jobs
# ----------------------------------------------------------------------

async def collect_job(ctx: TaskContext) -> Dict[str, Any]:
    """Collect using the active resume's query or the standalone search query."""
    resume = await ctx.repository.get_active_resume()
    query = (resume.search_query if resume else ctx.settings.search_query).strip()
    if not query:
        raise ValueError(
            "Не задан поисковый запрос — укажите должность в карточке «Искать вакансии»"
        )

    scroller_config = ctx.settings.scroller_config(query=query)
    search_url = scroller_config.search_url
    ctx.log(f"резюме: {resume.title or resume.source_url} — ищу «{query}»" if resume
            else f"поиск без резюме — ищу «{query}»")

    browser_config = await ensure_browser(ctx)
    run_id = f"web_{ctx.state.id}"
    ctx.log(f"сбор вакансий: {search_url}")
    ctx.log("собираю до конца выдачи или нажатия «Стоп», без лимита страниц")
    ctx.progress(0)

    await ctx.repository.create_search_run(
        SearchRun(
            id=run_id,
            task_id=run_id,
            search_url=search_url,
            browser_session_id=browser_config.endpoint,
            transport="playwright",
            status=SearchRunStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
            # Remembered so the vacancies list can show what each resume found.
            resume_id=resume.id if resume else None,
        )
    )

    collector = HHVacancyCardCollector()
    # The generator only yields once per search page, so without this hook a stop
    # request would sit unnoticed for the whole scroll of the current page.
    ctx.on_stop(collector.stop)
    summary: Optional[CollectionSummary] = None
    pages = 0

    _log_rate_limits(ctx)

    stream = collector.collect(
        browser_config=browser_config,
        search_url=search_url,
        task_id=run_id,
        scroller_config=scroller_config,
        limiter=ctx.limiter,
        on_retry=ctx.log,
    )

    detailed = detail_errors = new_vacancies = known_vacancies = skipped_details = 0

    def report_progress():
        ctx.state.result['collect_progress'] = {
            'pages': pages, 'new': new_vacancies, 'known': known_vacancies,
            'skipped_details': skipped_details, 'detailed': detailed, 'detail_errors': detail_errors,
        }
        ctx.progress(pages)

    report_progress()
    reader = VacancyDetailsReader(browser_config, limiter=ctx.limiter, should_stop=ctx.should_stop, on_retry=ctx.log)
    try:
        async with reader:
            async for item in stream:
                if isinstance(item, PageCommitParams):
                    existing = await ctx.repository.collection_card_state(item.cards)
                    # Commit the whole result page first. A detail failure or stop
                    # must never lose cards we have already discovered.
                    await ctx.repository.commit_page_transaction(item)
                    pages += 1
                    new_on_page = sum((card.source, card.external_id) not in existing for card in item.cards)
                    new_vacancies += new_on_page
                    known_vacancies += len(item.cards) - new_on_page
                    pending = [card for card in item.cards if not existing.get((card.source, card.external_id), False)]
                    skipped_details += len(item.cards) - len(pending)
                    ctx.log(f"страница #{item.page_number}: новых {new_on_page}, уже в базе {len(item.cards) - new_on_page}; подробностей к загрузке {len(pending)}")
                    report_progress()
                    for index, card in enumerate(pending, 1):
                        if ctx.should_stop():
                            break
                        ctx.log(f"подробности {index}/{len(pending)} на странице {pages}: {card.title}")
                        try:
                            details = await reader.read(card)
                        except HHInterventionRequired:
                            raise
                        except Exception as error:
                            details = VacancyDetails(error=str(error)[:300])
                            detail_errors += 1
                            ctx.log(f"не удалось прочитать вакансию {card.external_id}: {details.error}")
                        else:
                            detailed += 1
                        await ctx.repository.save_vacancy_details(card.source, card.external_id, details)
                        report_progress()
                elif isinstance(item, CollectionSummary):
                    summary = item

                if ctx.should_stop():
                    collector.stop()
    except asyncio.CancelledError:
        await ctx.repository.update_search_run_status(
            run_id, status=SearchRunStatus.CANCELLED, reason="stopped_by_user"
        )
        raise
    except Exception as e:
        await ctx.repository.update_search_run_status(
            run_id, status=SearchRunStatus.FAILED, error_code="ERR_COLLECT", error_message=str(e)
        )
        raise
    finally:
        await close_stream(stream)

    status = SearchRunStatus.CANCELLED if ctx.should_stop() else SearchRunStatus.COMPLETED
    await ctx.repository.update_search_run_status(
        run_id, status=status, reason=summary.completion_reason if summary else ""
    )

    result = {
        "run_id": run_id,
        "pages": summary.total_pages_processed if summary else pages,
        "cards": summary.total_cards_found if summary else 0,
        "unique": summary.unique_vacancies if summary else 0,
        "reason": summary.completion_reason if summary else "",
        "detailed": detailed,
        "detail_errors": detail_errors,
        "new": new_vacancies,
        "known": known_vacancies,
        "skipped_details": skipped_details,
        "collect_progress": ctx.state.result['collect_progress'],
        "rate_limit": ctx.limiter.stats.as_dict(),
    }
    ctx.log(f"новых вакансий: {new_vacancies}, уже в базе: {known_vacancies}; страниц: {result['pages']}; подробностей: {detailed}, пропущено сохранённых: {skipped_details}, ошибок чтения: {detail_errors}")
    return result


async def score_job(ctx: TaskContext) -> Dict[str, Any]:
    """Score collected vacancies against the active resume using the LLM."""
    resume = await ctx.repository.get_active_resume()
    if not resume:
        raise ValueError("Нет активного резюме — импортируйте его по ссылке на вкладке «Резюме»")

    llm_config = ctx.settings.llm_config()
    if not llm_config.enabled:
        raise ValueError("Нейросеть выключена — включите её в настройках")

    batch_size = int(ctx.settings.get("matching.batch_size", 50))
    concurrency = int(ctx.settings.get("matching.concurrency", 3))
    threshold = int(ctx.settings.get("matching.threshold", 70))
    prompt = str(ctx.settings.get("matching.prompt", ""))

    rows = await ctx.repository.get_unscored_vacancies(resume.id, limit=batch_size)
    if not rows:
        ctx.log("новых вакансий для оценки нет")
        return {"scored": 0, "failed": 0, "above_threshold": 0}

    ctx.log(f"оцениваю {len(rows)} вакансий моделью {llm_config.model} (порог {threshold})")
    ctx.progress(0, len(rows))

    stats = {"scored": 0, "failed": 0, "above_threshold": 0}

    async def on_result(score, row) -> None:
        await ctx.repository.upsert_score(score)
        if score.error_message:
            stats["failed"] += 1
            ctx.log(f"× {row.get('title', '')[:60]} — {score.error_message[:80]}")
        else:
            stats["scored"] += 1
            if score.score >= threshold:
                stats["above_threshold"] += 1
            mark = "✓" if score.score >= threshold else "·"
            ctx.log(f"{mark} {score.score:>3} — {row.get('title', '')[:60]}")
        ctx.progress(stats["scored"] + stats["failed"])

    async with create_llm_client(llm_config) as llm:
        scorer = VacancyScorer(llm, prompt=prompt, model_name=llm_config.model)
        await scorer.score_many(
            resume, rows, concurrency=concurrency, on_result=on_result, should_stop=ctx.should_stop
        )

    ctx.log(f"оценено: {stats['scored']}, выше порога: {stats['above_threshold']}, ошибок: {stats['failed']}")
    return stats


async def apply_job(ctx: TaskContext, vacancy_ids: Optional[Sequence[int]] = None) -> Dict[str, Any]:
    """Apply to vacancies: either an explicit selection or everything above the threshold."""
    resume = await ctx.repository.get_active_resume()
    threshold = int(ctx.settings.get("matching.threshold", 70))
    limit = int(ctx.settings.get("apply.batch_limit", 20))
    delay = float(ctx.settings.get("apply.delay_sec", 2.0))

    if vacancy_ids:
        targets = await ctx.repository.get_vacancies_by_ids(list(vacancy_ids))
        ctx.log(f"отклик на выбранные вакансии: {len(targets)}")
    else:
        if not resume:
            raise ValueError("Нет активного резюме — импортируйте его на вкладке «Резюме»")
        targets = await ctx.repository.get_vacancies_to_apply(resume.id, threshold, limit)
        ctx.log(f"отклик на вакансии с оценкой ≥ {threshold}: {len(targets)}")

    if not targets:
        ctx.log("подходящих вакансий для отклика нет")
        return {"total": 0, "applied": 0, "skipped": 0, "already_applied": 0, "failed": 0}

    browser_config = await ensure_browser(ctx)
    stats = {"total": len(targets), "applied": 0, "skipped": 0, "skipped_low_score": 0,
             "already_applied": 0, "failed": 0}
    ctx.progress(0, len(targets))
    _log_rate_limits(ctx)

    # The LLM is needed for two optional steps: re-checking the match on the full
    # vacancy page, and writing a cover letter when hh.ru asks for one.
    recheck = bool(ctx.settings.get("apply.recheck_with_llm", False))
    letter_config = ctx.settings.cover_letter_config()
    resume_text = resume.as_prompt_text() if resume else ""

    llm_client = None
    if resume and (recheck or letter_config.enabled):
        llm_config = ctx.settings.llm_config()
        if llm_config.enabled:
            llm_client = create_llm_client(llm_config)
            if recheck:
                ctx.log("включена перепроверка нейросетью по полному тексту вакансии")
        elif letter_config.enabled:
            ctx.log("нейросеть выключена — вакансии с сопроводительным письмом будут пропущены")

    letter_writer = build_writer(llm_client, letter_config, resume_text)
    if letter_writer:
        when = "всегда" if letter_config.when == "always" else "когда требуется"
        ctx.log(f"сопроводительное письмо: генерирую {when}, до {letter_config.max_chars} символов")

    applier = HHAutoApplier(
        llm_client=llm_client if recheck else None,
        resume_text=resume_text if (llm_client and recheck) else "",
        # HHAutoApplier works on a 1-10 scale; the UI threshold is 0-100.
        min_score=max(1, round(threshold / 10)),
        cover_letter_writer=letter_writer,
        cover_letter_when=letter_config.when,
        cover_letter_fallback=letter_config.fallback_text,
    )

    connector = BrowserConnector(browser_config, limiter=ctx.limiter)
    try:
        async with connector.connect() as page:
            for index, item in enumerate(targets, start=1):
                ctx.raise_if_stopped()
                # Vacancies are opened one at a time, at the configured pace.
                await ctx.pace()
                ctx.raise_if_stopped()
                ctx.log(f"[{index}/{len(targets)}] {item.get('title', '')[:70]}")

                result = await applier.apply_to_vacancy(
                    page,
                    vacancy_url=item.get("url", ""),
                    external_id=item.get("external_id", ""),
                    vacancy_id=item.get("id"),
                )
                await ctx.repository.record_application(result)

                if result.status == ApplicationStatus.APPLIED:
                    stats["applied"] += 1
                    ctx.log("  → отклик отправлен")
                elif result.status == ApplicationStatus.SKIPPED_QUESTIONS:
                    stats["skipped"] += 1
                    ctx.log("  → пропущено: требуются ответы на вопросы работодателя")
                elif result.status == ApplicationStatus.SKIPPED_LOW_SCORE:
                    stats["skipped_low_score"] += 1
                    ctx.log(f"  → пропущено при перепроверке: {result.response_text[:100]}")
                elif result.status == ApplicationStatus.ALREADY_APPLIED:
                    stats["already_applied"] += 1
                    ctx.log("  → уже был отклик")
                else:
                    stats["failed"] += 1
                    ctx.log(f"  → ошибка: {result.error_message[:100]}")

                ctx.progress(index)
                if delay and index < len(targets):
                    await asyncio.sleep(delay)
    finally:
        if llm_client:
            await llm_client.aclose()

    ctx.log(
        f"итог: отправлено {stats['applied']}, пропущено {stats['skipped']}, "
        f"уже были {stats['already_applied']}, ошибок {stats['failed']}"
    )
    stats["rate_limit"] = ctx.limiter.stats.as_dict()
    return stats


async def resume_import_job(ctx: TaskContext, resume_id: Optional[int] = None) -> Dict[str, Any]:
    """Discover all account resumes, or re-parse one saved card."""
    await ensure_browser(ctx)
    service = ResumeService(ctx.repository, ctx.settings, limiter=ctx.limiter)
    if resume_id is not None:
        ctx.log("заново читаю резюме с hh.ru")
        await ctx.pace()
        resume = await service.refresh(resume_id)
        ctx.log(f"резюме обновлено: {resume.title}")
        return {"resume_id": resume.id, "title": resume.title, "imported": 1, "failed": 0}
    ctx.log("ищу все резюме в аккаунте hh.ru")
    report = await service.import_account(checkpoint=ctx.pace, log=ctx.log, progress=ctx.progress)
    ctx.log(f"импорт завершён: сохранено {report['imported']}, ошибок {report['failed']}")
    return report


async def login_job(ctx: TaskContext) -> Dict[str, Any]:
    """Confirm the actual hh.ru account and persist it with the login run."""
    browser_config = await ensure_browser(ctx)
    async with SESSION.connection(browser_config) as (browser, context):
        switching = bool(ctx.state.params.get("switch_account"))
        # Keep the working profile intact while the user signs into another account.
        login_context = await browser.new_context(no_viewport=True) if switching else context
        # Reuse the existing login/challenge tab without interrupting its state.
        pages = [page for page in login_context.pages if not page.is_closed() and page not in LIVE_PAGES]
        page = next((page for page in reversed(pages) if is_hh_url(page.url)), None)
        if page is None:
            page = next((page for page in pages if page.url in ("about:blank", "about:newtab")), None)
        created_page = page is None
        if created_page:
            page = await login_context.new_page()
        storage_path = Path(browser_config.storage_state_path).resolve()
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            ctx.raise_if_stopped()
            if switching:
                ctx.log("войдите в другой аккаунт в отдельном окне — прежняя сессия сохранена; «Стоп» отменит смену")
                account = {"logged_in": False, "account_name": ""}
            else:
                # On a fresh launch the reused tab may still be loading. The
                # authenticated state is in its HTML, so no menu click is needed.
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
                account = await read_hh_account(page)
            if not account["logged_in"]:
                if not intervention_message(page.url):
                    await page.goto("https://hh.ru/account/login", wait_until="domcontentloaded")
                ctx.log("войдите в аккаунт в окне браузера — вход определится автоматически")
                ctx.log("если статус не обновился, нажмите «Я вошёл» в интерфейсе")
                account = await _wait_for_hh_login(ctx, page)
            ctx.raise_if_stopped()
            if switching:
                account = await _commit_hh_account(ctx, login_context, context, storage_path)
            else:
                await _save_hh_session(context, storage_path, checkpoint=ctx.raise_if_stopped)
            # Login remains confirmed even if the subsequent resume import is stopped.
            ctx.state.result = {"storage_state": str(storage_path), "account_verified": True, **account}
            await ctx.repository.save_settings({"hh.login_required": "false"})
        except (Exception, asyncio.CancelledError):
            # Leave reused tabs alone, including when verification fails. A
            # successful login tab stays open for the user and the next check.
            if created_page and not switching:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass
            raise
        finally:
            if switching:
                try:
                    await login_context.close()
                except Exception:
                    logger.warning("could not close the temporary login window")

    ctx.log("вход в hh.ru подтверждён" + (f": {account['account_name']}" if account["account_name"] else ""))
    result = dict(ctx.state.result)
    try:
        result["resume_import"] = await resume_import_job(ctx)
    except Exception as error:
        # A parsing failure must not erase an independently confirmed login.
        ctx.log(f"Вход выполнен, но автоимпорт не завершён: {error}. Повторите на странице «Резюме».")
        result["resume_import"] = {"error": str(error)}
    return result


async def _commit_hh_account(ctx, source, target, storage_path: Path) -> dict:
    """Install and verify the new hh.ru cookies, rolling back on cancellation or failure."""
    cookies = [cookie for cookie in await source.cookies() if HH_COOKIE_DOMAIN.search(cookie["domain"])]
    if not cookies:
        raise ValueError("Новая сессия hh.ru не найдена. Прежний аккаунт сохранён.")
    previous = [cookie for cookie in await target.cookies() if HH_COOKIE_DOMAIN.search(cookie["domain"])]
    verification = await target.new_page()
    try:
        ctx.raise_if_stopped()
        await target.clear_cookies(domain=HH_COOKIE_DOMAIN)
        await target.add_cookies(cookies)
        await verification.goto("https://hh.ru/", wait_until="domcontentloaded", timeout=15000)
        account = await read_hh_account(verification)
        if not account["logged_in"]:
            raise ValueError("Не удалось перенести вход в основной браузер. Прежний аккаунт сохранён.")
        ctx.raise_if_stopped()
        await _save_hh_session(target, storage_path, checkpoint=ctx.raise_if_stopped)
        return account
    except (Exception, asyncio.CancelledError):
        async def restore():
            await target.clear_cookies(domain=HH_COOKIE_DOMAIN)
            await target.add_cookies(previous)

        restoring = asyncio.create_task(restore())
        try:
            await asyncio.shield(restoring)
        except asyncio.CancelledError:
            await restoring
        except Exception:
            ctx.state.result = {"session_preserved": False}
            ctx.log("Не удалось восстановить прежнюю сессию в браузере. Сохранённая копия не изменена.")
        raise
    finally:
        try:
            await verification.close()
        except Exception:
            pass


async def _wait_for_hh_login(ctx: TaskContext, page) -> Dict[str, Any]:
    """Watch the login tab without interrupting typing or SMS verification."""
    confirmation = asyncio.create_task(ctx.wait_for_confirmation(timeout_sec=900))
    try:
        while not confirmation.done():
            ctx.raise_if_stopped()
            if page.is_closed():
                raise RuntimeError("Браузер HH был закрыт или перезапущен. Закройте окно входа и откройте браузер снова.")
            account = await read_hh_account(page)
            if account["logged_in"]:
                return account
            await asyncio.wait({confirmation}, timeout=2)
        if not await confirmation:
            raise TimeoutError("вход не подтверждён за 15 минут")
        # Manual confirmation is a request to check, not proof of login.
        await page.goto("https://hh.ru/", wait_until="domcontentloaded", timeout=15000)
        try:
            await page.locator(", ".join(LOGIN_SELECTORS)).first.wait_for(state="visible", timeout=5000)
        except Exception:
            pass
        account = await read_hh_account(page)
        if not account["logged_in"]:
            raise ValueError("Вход в hh.ru не подтверждён. Завершите вход в браузере и попробуйте снова.")
        return account
    finally:
        confirmation.cancel()
        await asyncio.gather(confirmation, return_exceptions=True)


async def activity_job(ctx: TaskContext) -> Dict[str, Any]:
    """Browse hh.ru like a human, in parallel with the collecting jobs."""
    browser_config = await ensure_browser(ctx)
    # Browse what the active resume is hunting for, unless a URL is pinned.
    resume = await ctx.repository.get_active_resume()
    activity_config = ctx.settings.activity_config(query=resume.search_query if resume else None)
    if not activity_config.url:
        raise ValueError("Не задан адрес для имитации активности — укажите его в настройках")

    ctx.log(
        f"имитация активности на {activity_config.duration_min:.0f} мин "
        f"(пауза {activity_config.pause_min_sec:.1f}–{activity_config.pause_max_sec:.1f} с)"
    )
    scroller = ActivityScroller(activity_config, limiter=ctx.limiter)
    report = await scroller.run(
        browser_config,
        should_stop=ctx.should_stop,
        log=ctx.log,
        progress=ctx.progress,
    )
    return report.as_dict()


async def profile_job(
    ctx: TaskContext, resume_id: Optional[int] = None, model: str = ""
) -> Dict[str, Any]:
    """Rebuild the condensed candidate profile from the resume and its context."""
    resume = (
        await ctx.repository.get_resume(resume_id)
        if resume_id
        else await ctx.repository.get_active_resume()
    )
    if not resume:
        raise ValueError("Резюме не найдено")

    llm_config = ctx.settings.llm_config()
    if not llm_config.enabled:
        raise ValueError("Нейросеть выключена — включите её в настройках")

    profile_config = ctx.settings.profile_config()
    if model:
        # Picked right next to the button on the resume card.
        profile_config = replace(profile_config, model=model)
    ctx.log(f"профиль для «{resume.title or resume.source_url}», модель {profile_config.model}")

    async with create_llm_client(llm_config) as llm:
        builder = ProfileBuilder(llm, profile_config)
        result = await builder.build(resume, log=ctx.log)

    await ctx.repository.save_resume_profile(
        resume.id, result.text, result.model, resume.content_fingerprint()
    )
    return {
        "resume_id": resume.id,
        "chars": len(result.text),
        "source_chars": result.source_chars,
        "chunks": result.chunks,
        "model": result.model,
    }


async def resume_touch_job(ctx: TaskContext) -> Dict[str, Any]:
    """Refresh the resume so hh.ru lifts its date without changing the content."""
    resume = await ctx.repository.get_active_resume()
    if not resume or not resume.source_url:
        raise ValueError("Нет активного резюме — импортируйте его на вкладке «Резюме»")

    browser_config = await ensure_browser(ctx)
    await ctx.pace()

    toucher = ResumeToucher(allow_edit_fallback=bool(ctx.settings.get("resume_touch.edit_fallback", False)))
    connector = BrowserConnector(browser_config, limiter=ctx.limiter)
    async with connector.connect() as page:
        result = await toucher.touch(page, resume.source_url, log=ctx.log)

    if not result.get("updated"):
        ctx.log("резюме не обновлено — вероятно, ещё не прошёл интервал hh.ru")
    return result


async def pipeline_job(ctx: TaskContext) -> Dict[str, Any]:
    """Collect -> score -> (optionally) apply, as configured in the settings."""
    result: Dict[str, Any] = {}

    if bool(ctx.settings.get("schedule.do_collect", True)):
        ctx.log("=== этап 1: сбор вакансий ===")
        result["collect"] = await collect_job(ctx)
        ctx.raise_if_stopped()

    if bool(ctx.settings.get("schedule.do_score", True)) and bool(ctx.settings.get("matching.enabled", True)):
        ctx.log("=== этап 2: оценка соответствия ===")
        result["score"] = await score_job(ctx)
        ctx.raise_if_stopped()

    if bool(ctx.settings.get("schedule.do_apply", False)):
        if ctx.settings.get("apply.mode", "manual") != "auto":
            ctx.log("этап откликов пропущен: режим откликов «ручной»")
        else:
            ctx.log("=== этап 3: отклики ===")
            result["apply"] = await apply_job(ctx)

    return result
