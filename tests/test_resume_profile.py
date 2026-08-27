"""Per-resume search query, free-form context and the condensed profile."""

import pytest
from anyio.from_thread import start_blocking_portal
from fastapi.testclient import TestClient

from src.config import Config
from src.db.connection import init_sqlite
from src.db.models import Resume, SearchRun
from src.db.repository import DatabaseRepository
from src.llm import ChatResponse, LLMError
from src.resume.profile import ProfileBuilder, ProfileConfig, ProfileError, _split
from src.web.app import create_app


class FakeLLM:
    def __init__(self, replies=None, error=None):
        self.replies = list(replies or [])
        self.error = error
        self.requests = []

    async def chat(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        text = self.replies.pop(0) if self.replies else "профиль " * 60
        return ChatResponse(text=text)

    async def aclose(self):
        pass


@pytest.fixture()
async def repo(tmp_path):
    conn = await init_sqlite(str(tmp_path / "profile.db"))
    yield DatabaseRepository(conn, driver="sqlite")
    await conn.close()


# --- the profile itself --------------------------------------------------

async def test_profile_is_built_from_resume_and_context():
    resume = Resume(
        raw_text="Инженер поддержки. Python, PostgreSQL.",
        context_text="Ещё вёл проект миграции на Kubernetes, о котором в резюме не написано.",
    )
    llm = FakeLLM(["Кандидат: инженер поддержки. Python, PostgreSQL, Kubernetes. " * 8])
    result = await ProfileBuilder(llm, ProfileConfig(model="heavy-model")).build(resume)

    assert "Kubernetes" in result.text
    assert result.model == "heavy-model"
    assert result.chunks == 1

    prompt = llm.requests[0].messages[0].content
    assert "Python, PostgreSQL" in prompt        # the hh.ru resume
    assert "миграции на Kubernetes" in prompt    # the hand-written context
    assert llm.requests[0].model == "heavy-model"


async def test_long_source_is_summarised_in_chunks_then_merged():
    resume = Resume(raw_text="Опыт работы. " * 900, context_text="Проекты. " * 900)
    llm = FakeLLM(["часть " * 60, "часть " * 60, "сводный профиль " * 40])

    result = await ProfileBuilder(llm, ProfileConfig()).build(resume)

    assert result.chunks >= 2
    assert len(llm.requests) == result.chunks + 1  # one merge pass on top
    assert "сводный" in result.text


async def test_empty_source_is_refused():
    with pytest.raises(ProfileError):
        await ProfileBuilder(FakeLLM(), ProfileConfig()).build(Resume())


async def test_llm_failure_is_reported_as_profile_error():
    resume = Resume(raw_text="Инженер поддержки.")
    with pytest.raises(ProfileError) as failure:
        await ProfileBuilder(FakeLLM(error=LLMError("нет связи")), ProfileConfig()).build(resume)
    assert "нейросеть" in str(failure.value)


async def test_too_short_profile_is_refused():
    resume = Resume(raw_text="Инженер поддержки.")
    with pytest.raises(ProfileError) as failure:
        await ProfileBuilder(FakeLLM(["коротко"]), ProfileConfig()).build(resume)
    assert "коротким" in str(failure.value)


async def test_profile_is_clamped_to_the_configured_length():
    resume = Resume(raw_text="Инженер.")
    llm = FakeLLM(["длинный профиль " * 500])
    result = await ProfileBuilder(llm, ProfileConfig(max_chars=900)).build(resume)
    assert len(result.text) <= 900


def test_split_keeps_paragraphs_whole():
    text = "\n\n".join(f"Абзац номер {i}." for i in range(50))
    chunks = _split(text, 200)
    assert len(chunks) > 1
    assert all(len(chunk) <= 200 for chunk in chunks)
    assert "Абзац номер 49." in chunks[-1]


# --- the profile is what reaches the model -------------------------------

def test_prompt_text_prefers_the_profile():
    resume = Resume(
        title="Инженер",
        skills=["Python"],
        raw_text="сырой текст резюме",
        profile_summary="СЖАТЫЙ ПРОФИЛЬ: инженер поддержки, Python, Kubernetes.",
    )
    prompt = resume.as_prompt_text()

    assert prompt.startswith("СЖАТЫЙ ПРОФИЛЬ")
    assert "сырой текст резюме" not in prompt  # the raw page is no longer needed


def test_prompt_text_falls_back_without_a_profile():
    resume = Resume(title="Инженер", skills=["Python"], raw_text="сырой текст")
    assert "Инженер" in resume.as_prompt_text()


def test_stale_profile_is_detected():
    resume = Resume(raw_text="текст", context_text="контекст")
    resume.profile_summary = "профиль"
    resume.profile_hash = resume.content_fingerprint()
    assert resume.profile_is_stale is False

    resume.context_text = "контекст изменился"
    assert resume.profile_is_stale is True


# --- storage -------------------------------------------------------------

async def test_query_context_and_profile_survive_a_reimport(repo):
    resume_id = await repo.upsert_resume(Resume(source_url="https://hh.ru/resume/a", title="Инженер"))
    await repo.update_resume_fields(resume_id, "Python разработчик", "мой опыт")
    await repo.save_resume_profile(resume_id, "профиль кандидата", "heavy-model", "hash123")

    stored = await repo.get_resume(resume_id)
    assert stored.search_query == "Python разработчик"
    assert stored.context_text == "мой опыт"
    assert stored.profile_summary == "профиль кандидата"
    assert stored.profile_model == "heavy-model"
    assert stored.profile_updated_at is not None


async def test_several_resumes_keep_their_own_queries(repo):
    first = await repo.upsert_resume(Resume(source_url="https://hh.ru/resume/1", title="Поддержка"))
    second = await repo.upsert_resume(Resume(source_url="https://hh.ru/resume/2", title="QA"))
    await repo.update_resume_fields(first, "инженер поддержки", "контекст 1")
    await repo.update_resume_fields(second, "QA automation", "контекст 2")
    await repo.set_active_resume(second)

    resumes = {r.id: r for r in await repo.list_resumes()}
    assert resumes[first].search_query == "инженер поддержки"
    assert resumes[second].search_query == "QA automation"
    assert (await repo.get_active_resume()).id == second


async def test_vacancies_can_be_filtered_by_the_resume_that_found_them(repo):
    from src.db.models import PageCommitParams, VacancyCard

    first = await repo.upsert_resume(Resume(source_url="https://hh.ru/resume/1"))
    second = await repo.upsert_resume(Resume(source_url="https://hh.ru/resume/2"))

    async def collect(run_id, resume_id, external_id):
        await repo.create_search_run(
            SearchRun(id=run_id, task_id=run_id, search_url="u", resume_id=resume_id)
        )
        await repo.commit_page_transaction(PageCommitParams(
            search_run_id=run_id, page_key="p1", page_number=1, current_url="u", canonical_url="u",
            cards=[VacancyCard(external_id=external_id, url=f"https://hh.ru/vacancy/{external_id}",
                               title=f"Вакансия {external_id}")],
        ))

    await collect("run-1", first, "v1")
    await collect("run-2", second, "v2")

    for_first = await repo.list_vacancies(found_for_resume=first)
    for_second = await repo.list_vacancies(found_for_resume=second)
    everything = await repo.list_vacancies()

    assert [row["external_id"] for row in for_first] == ["v1"]
    assert [row["external_id"] for row in for_second] == ["v2"]
    assert len(everything) == 2
    assert await repo.count_vacancies(found_for_resume=first) == 1


# --- web ------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path):
    config = Config.load("configs/config.local.yaml")
    config.db.sqlite_path = str(tmp_path / "web.db")
    with TestClient(create_app(config)) as test_client:
        yield test_client


def test_resume_card_offers_query_and_context(client):
    with start_blocking_portal() as portal:
        portal.call(client.app.state.repository.upsert_resume,
                    Resume(source_url="https://hh.ru/resume/x", title="Инженер"))

    page = client.get("/resume").text
    assert 'name="search_query"' in page
    assert 'name="context_text"' in page
    assert "Расскажите про свой опыт" in page
    assert "Собрать профиль" in page


def test_query_and_context_are_saved_from_the_card(client):
    with start_blocking_portal() as portal:
        resume_id = portal.call(client.app.state.repository.upsert_resume,
                                Resume(source_url="https://hh.ru/resume/y"))

    client.post(
        f"/actions/resume/{resume_id}/update",
        data={"search_query": "Go разработчик", "context_text": "писал сервисы на Go"},
        follow_redirects=False,
    )

    with start_blocking_portal() as portal:
        stored = portal.call(client.app.state.repository.get_resume, resume_id)
    assert stored.search_query == "Go разработчик"
    assert stored.context_text == "писал сервисы на Go"


def test_search_query_is_no_longer_a_global_setting(client):
    settings_page = client.get("/settings").text
    assert 'name="search.query"' not in settings_page
    assert "Профиль кандидата" in settings_page
    assert 'name="profile.model"' in settings_page


def test_profile_settings_round_trip(client):
    client.post(
        "/actions/settings",
        data={"profile.model": "heavy-model", "profile.max_chars": "5000",
              "profile.prompt": "Собери профиль сухо."},
        follow_redirects=False,
    )
    config = client.app.state.settings.profile_config()
    assert config.model == "heavy-model"
    assert config.max_chars == 5000
    assert config.prompt == "Собери профиль сухо."


def test_collect_needs_a_query_on_the_resume(client):
    with start_blocking_portal() as portal:
        resume_id = portal.call(client.app.state.repository.upsert_resume,
                                Resume(source_url="https://hh.ru/resume/z"))
        portal.call(client.app.state.repository.set_active_resume, resume_id)

    response = client.post("/actions/start", data={"kind": "collect"})
    assert response.status_code == 200

    # The job runs in the background; wait for it to record its outcome.
    import anyio

    async def wait_for_failure():
        for _ in range(40):
            runs = await client.app.state.repository.list_task_runs(3)
            if runs and runs[0]["error_message"]:
                return runs[0]["error_message"]
            await anyio.sleep(0.05)
        return ""

    with start_blocking_portal() as portal:
        message = portal.call(wait_for_failure)
    assert "поисковый запрос" in message


def test_profile_runs_in_its_own_lane(client):
    from src.web.tasks import LANE_PROFILE

    assert LANE_PROFILE in client.app.state.tasks.lanes
    panel = client.get("/partials/status").text
    assert 'value="profile"' in panel
