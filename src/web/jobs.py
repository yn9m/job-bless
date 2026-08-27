"""Background jobs triggered from the web UI and by the scheduler."""

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import httpx
from playwright.async_api import async_playwright

from src.activity.service import ActivityScroller
from src.applier.auto_applier import HHAutoApplier
from src.applier.cover_letter import build_writer
from src.browser.connector import BrowserConnector
from src.browser.local_process import LocalProcessLauncher
from src.collector.collector import HHVacancyCardCollector
from src.config import BrowserConfig
from src.db.models import (
    ApplicationStatus,
    CollectionSummary,
    PageCommitParams,
    SearchRun,
    SearchRunStatus,
)
from src.llm import create_llm_client
from src.matching.scorer import VacancyScorer
from src.resume.profile import ProfileBuilder
from src.resume.service import ResumeService
from src.resume.toucher import ResumeToucher
from src.web.tasks import TaskContext

logger = logging.getLogger(__name__)

STORAGE_STATE_PATH = "./data/storage_state.json"


# ----------------------------------------------------------------------
# browser helpers
# ----------------------------------------------------------------------

async def _cdp_alive(endpoint: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(f"{endpoint.rstrip('/')}/json/version")
            return response.status_code == 200
    except httpx.HTTPError:
        return False


async def close_stream(stream) -> None:  # noqa: ANN001
    """Finalize an async generator so its browser tab is released now.

    An abandoned generator is only closed by the garbage collector, which can
    leave a Chrome tab hanging around for minutes after «Стоп». When the job
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


# Lanes start in parallel; without this both could launch Chrome at once.
_browser_lock = asyncio.Lock()


async def ensure_browser(ctx: TaskContext) -> BrowserConfig:
    """Make sure a browser we can attach to is running; return its config."""
    browser_config = ctx.settings.browser_config()
    if browser_config.provider != "local_process":
        return browser_config

    async with _browser_lock:
        if await _cdp_alive(browser_config.cdp.endpoint):
            ctx.log(f"браузер уже запущен ({browser_config.cdp.endpoint})")
            return browser_config

        ctx.log("запускаю Chrome...")
        launcher = LocalProcessLauncher(browser_config)
        endpoint, pid = launcher.start()
        browser_config.cdp.endpoint = endpoint

        for _ in range(20):  # Chrome needs a moment before the CDP port answers.
            if await _cdp_alive(endpoint):
                break
            await asyncio.sleep(0.5)
        ctx.log(f"Chrome готов (pid={pid}, {endpoint})")
        return browser_config


# ----------------------------------------------------------------------
# jobs
# ----------------------------------------------------------------------

async def collect_job(ctx: TaskContext) -> Dict[str, Any]:
    """Collect vacancies for the active resume, using its own search query."""
    resume = await ctx.repository.get_active_resume()
    if not resume:
        raise ValueError("Нет активного резюме — импортируйте его на вкладке «Резюме»")
    if not resume.search_query.strip():
        raise ValueError(
            f"У резюме «{resume.title or resume.source_url}» не задан поисковый запрос — "
            "укажите его на вкладке «Резюме»"
        )

    scroller_config = ctx.settings.scroller_config(query=resume.search_query)
    search_url = scroller_config.search_url
    ctx.log(f"резюме: {resume.title or resume.source_url} — ищу «{resume.search_query}»")

    browser_config = await ensure_browser(ctx)
    run_id = f"web_{ctx.state.id}"
    ctx.log(f"сбор вакансий: {search_url}")
    ctx.progress(0, scroller_config.max_pages)

    await ctx.repository.create_search_run(
        SearchRun(
            id=run_id,
            task_id=run_id,
            search_url=search_url,
            browser_session_id=browser_config.cdp.endpoint,
            transport=browser_config.transport,
            status=SearchRunStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
            # Remembered so the vacancies list can show what each resume found.
            resume_id=resume.id,
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
    )

    try:
        async for item in stream:
            if isinstance(item, PageCommitParams):
                await ctx.repository.commit_page_transaction(item)
                pages += 1
                ctx.log(f"страница #{item.page_number}: сохранено карточек {len(item.cards)}")
                ctx.progress(pages, scroller_config.max_pages)
            elif isinstance(item, CollectionSummary):
                summary = item

            if ctx.should_stop():
                collector.stop()
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
        "rate_limit": ctx.limiter.stats.as_dict(),
    }
    ctx.log(f"собрано уникальных вакансий: {result['unique']} со страниц: {result['pages']}")
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


async def resume_import_job(ctx: TaskContext, resume_url: str) -> Dict[str, Any]:
    """Import a resume from an hh.ru link using the logged-in browser."""
    await ensure_browser(ctx)
    ctx.log(f"импортирую резюме: {resume_url}")
    await ctx.pace()

    service = ResumeService(ctx.repository, ctx.settings, limiter=ctx.limiter)
    resume = await service.import_from_url(resume_url, activate=True)

    ctx.log(f"резюме сохранено: {resume.title or resume.full_name or resume.source_url}")
    if resume.skills:
        ctx.log(f"навыков распознано: {len(resume.skills)}")
    return {
        "resume_id": resume.id,
        "title": resume.title,
        "skills": len(resume.skills),
        "chars": len(resume.raw_text),
    }


async def login_job(ctx: TaskContext) -> Dict[str, Any]:
    """Open hh.ru login page in a real Chrome window and wait for the user."""
    browser_config = await ensure_browser(ctx)
    if browser_config.headless:
        raise ValueError("Для входа нужен видимый браузер — выключите headless в настройках")

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(
            browser_config.cdp.endpoint, timeout=browser_config.cdp.timeout_ms
        )
        contexts = browser.contexts
        context = contexts[0] if contexts else await browser.new_context()
        page = await context.new_page()
        await page.goto("https://hh.ru/account/login", wait_until="domcontentloaded")

        ctx.log("окно Chrome открыто на странице входа hh.ru")
        ctx.log("войдите в аккаунт и нажмите кнопку «Я вошёл» в интерфейсе")

        confirmed = await ctx.wait_for_confirmation(timeout_sec=900)
        if not confirmed:
            raise TimeoutError("вход не подтверждён за 15 минут")

        storage_path = Path(STORAGE_STATE_PATH).resolve()
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(storage_path))
        logged_in = await _looks_logged_in(page)
        try:
            await page.close()
        except Exception:  # noqa: BLE001
            pass

    ctx.log(f"сессия сохранена в {storage_path}")
    if not logged_in:
        ctx.log("предупреждение: не удалось подтвердить вход на странице — проверьте вручную")
    return {"storage_state": str(storage_path), "logged_in": logged_in}


async def _looks_logged_in(page) -> bool:
    try:
        await page.goto("https://hh.ru/", wait_until="domcontentloaded", timeout=15000)
        for selector in (
            '[data-qa="mainmenu_applicantProfile"]',
            '[data-qa="mainmenu_myResumes"]',
            '[data-qa="mainmenu_negotiations"]',
        ):
            if await page.query_selector(selector):
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


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
