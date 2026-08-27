"""Splitting hh.ru resume text into sections.

The fixture mimics the structure of an hh.ru page (headings, buttons, promo
blocks) with invented content.
"""

import pytest

from src.db.models import Resume
from src.resume.sections import parse_sections

RESUME_TEXT = """Профиль
/
Инженер по тестированию
Инженер по тестированию
Уровень дохода не указан
Тип занятости: Постоянная работа
Редактировать
Контакты
Мобильный телефон — предпочитаемый способ связи
+7 000 000-00-00
Опыт работы: 4 года 2 месяца
Добавить
ООО Пример
2 года и 1 месяц
Инженер по тестированию
Январь 2023 — Февраль 2025 (2 года и 1 месяц)
Чем занимался:
- Писал автотесты на Python.
- Поддерживал стенды.

Стек: Python, pytest, Docker, PostgreSQL.
Развернуть
ООО Другая компания
2 года и 1 месяц
Младший тестировщик
Январь 2021 — Январь 2023 (2 года и 1 месяц)
Ручное тестирование веб-приложений.
Развернуть
Навыки
Продвинутый уровень
Python
pytest
Docker
PostgreSQL
Git
Средний уровень
Linux
Указать уровни
Редактировать
Образование
Добавить
Университет Примерный, Москва
Программная инженерия
2020 · Бакалавр
Подтверждение навыков
Подтверждайте навыки — это выделит вас среди других кандидатов
Python
Docker
...
Перейти к тестам
Инженер по тестированию: куда расти дальше?
И где можно зарабатывать больше
Посмотреть направления
О себе
Люблю разбираться в сложных системах.

Английский - B2
❯ Рассматриваю удалёнку

Развернуть
Сертификаты
Добавить
ISTQB Foundation Level
2022
Certified Kubernetes Administrator
2021
По-русски
Завершённость резюме
Ещё вы можете добавить
Фотографию
Портфолио
Видимость резюме
Видно всем работодателям, зарегистрированным на hh.ru
Подобрали для вас 1234 подходящих вакансий
Сертификаты о ваших подтверждённых навыках
PostgreSQL
Получить на Госуслугах
"""


@pytest.fixture()
def sections():
    return parse_sections(RESUME_TEXT)


def test_skills_are_extracted_without_ui_noise(sections):
    assert sections.skills == ["Python", "pytest", "Docker", "PostgreSQL", "Git", "Linux"]
    for noise in ("Указать уровни", "Редактировать", "Продвинутый уровень", "Средний уровень"):
        assert noise not in sections.skills


def test_education_stops_before_the_promo_block(sections):
    assert "Университет Примерный, Москва" in sections.education
    assert "2020 · Бакалавр" in sections.education
    # The "verify your skills" promo directly follows the education block.
    assert "Подтверждайте навыки" not in sections.education
    assert "Перейти к тестам" not in sections.education


def test_certificates_keep_their_years(sections):
    assert sections.certificates == [
        "ISTQB Foundation Level (2022)",
        "Certified Kubernetes Administrator (2021)",
    ]
    assert "По-русски" not in " ".join(sections.certificates)


def test_summary_is_complete_and_free_of_buttons(sections):
    assert sections.summary.startswith("Люблю разбираться")
    assert "Английский - B2" in sections.summary
    assert "Рассматриваю удалёнку" in sections.summary
    assert "Развернуть" not in sections.summary
    assert "Сертификаты" not in sections.summary


def test_experience_covers_every_position_including_the_last_stack_line(sections):
    assert "Инженер по тестированию" in sections.experience
    assert "Младший тестировщик" in sections.experience
    # The tail of a position description used to be cut off mid-sentence.
    assert "Стек: Python, pytest, Docker, PostgreSQL." in sections.experience
    assert "Ручное тестирование веб-приложений." in sections.experience
    assert "Навыки" not in sections.experience


def test_empty_and_garbage_input_is_safe():
    assert parse_sections("").skills == []
    assert parse_sections("   \n\n  ").summary == ""
    only_prose = parse_sections("Просто текст без заголовков разделов")
    assert only_prose.skills == [] and only_prose.education == ""


def test_missing_sections_do_not_break_the_rest():
    text = "Опыт работы: 1 год\nООО Компания\nРазработчик\nНавыки\nGo\nDocker\n"
    parsed = parse_sections(text)
    assert parsed.skills == ["Go", "Docker"]
    assert "Разработчик" in parsed.experience
    assert parsed.education == "" and parsed.certificates == []


def test_prompt_text_carries_every_section():
    resume = Resume(
        title="Инженер по тестированию",
        skills=["Python", "Docker"],
        experience_text="Инженер по тестированию\nСтек: Python.",
        education_text="Университет Примерный\n2020 · Бакалавр",
        certificates=["ISTQB Foundation Level (2022)"],
        summary="Люблю разбираться в сложных системах.",
        raw_text=RESUME_TEXT,
    )
    prompt = resume.as_prompt_text()

    for expected in ("Инженер по тестированию", "Python, Docker", "Университет Примерный",
                     "ISTQB Foundation Level", "Люблю разбираться"):
        assert expected in prompt
    assert "Полный текст резюме:" in prompt  # nothing recognised is dropped


def test_prompt_text_respects_the_limit():
    resume = Resume(title="Инженер", summary="а" * 5000, raw_text="б" * 50000)
    prompt = resume.as_prompt_text(max_chars=2000)
    assert len(prompt) <= 2000


def test_prompt_text_falls_back_to_raw_when_nothing_structured():
    resume = Resume(raw_text="Только сырой текст резюме")
    assert resume.as_prompt_text() == "Только сырой текст резюме"
