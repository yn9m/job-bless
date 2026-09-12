"""Settings owned by individual actions and the remaining shared settings."""

from src.settings import FIELDS


def _prefix(prefix):
    return tuple(field.key for field in FIELDS if field.key.startswith(prefix))


ACTION_SECTIONS = {
    "llm": (("Нейросеть", _prefix("llm.")),),
    "search": (("Поиск вакансий", _prefix("scroller.")),),
    "score": (("Оценка вакансий", ("matching.batch_size", "matching.concurrency", "matching.prompt")),),
    "apply": (
        ("Отбор и отправка", ("matching.threshold", "apply.batch_limit", "apply.delay_sec", "apply.recheck_with_llm")),
        ("Сопроводительное письмо", _prefix("cover_letter.")),
    ),
    "profile": (("Описание опыта", _prefix("profile.")),),
    "resume_touch": (
        ("Поднятие резюме", ("resume_touch.edit_fallback",)),
        ("Расписание", ("schedule.resume_touch_enabled", "schedule.resume_touch_interval_hours")),
    ),
    "pipeline": (("Расписание", ("schedule.enabled", "schedule.interval_minutes")),),
}

ACTION_KEYS = {
    kind: {key for _, keys in sections for key in keys}
    for kind, sections in ACTION_SECTIONS.items()
}
PIPELINE_KEYS = ACTION_KEYS["pipeline"] | {
    "schedule.do_collect", "schedule.do_score", "schedule.do_apply", "matching.enabled", "apply.mode",
}
# The threshold is shared by scoring, filtering, and sending replies. The
# question toggle has no runtime consumer and is deliberately not offered.
SHARED_KEYS = ({field.key for field in FIELDS} - set().union(*ACTION_KEYS.values()) - PIPELINE_KEYS
               - {"apply.skip_questions", "schedule.activity_enabled", "schedule.activity_interval_minutes"}
               - set(_prefix("activity."))) | {"matching.threshold"}


def shared_groups(settings):
    groups = []
    for title, fields in settings.grouped_fields():
        visible = {level: [field for field in rows if field["key"] in SHARED_KEYS]
                   for level, rows in fields.items()}
        if any(visible.values()):
            groups.append(("Отбор вакансий" if title == "Оценка соответствия" else title, visible))
    return groups


def action_sections(settings, kind, raw=None):
    fields = {field["key"]: dict(field)
              for _, group in settings.grouped_fields()
              for field in group["main"] + group["advanced"]}
    fields["scroller.load_mode"]["choice_labels"] = {"instant": "Читать сразу", "scroll": "Прокручивать страницу"}
    fields["matching.threshold"]["help"] = "Минимальная оценка от 0 до 100. Этот же порог используется в счётчиках подходящих вакансий."
    fields["apply.batch_limit"]["help"] = "Лимит при автоматическом отборе по оценке. При отправке из списка вакансий обрабатывается весь ваш выбор."
    fields["cover_letter.when"]["choice_labels"] = {"required": "Только когда требуется", "always": "Всегда"}
    if raw is not None:
        for key in ACTION_KEYS[kind]:
            field = fields[key]
            field["value"] = raw.get(key) == "1" if field["type"] == "bool" else raw.get(key, field["value"])
    return [(title, [fields[key] for key in keys]) for title, keys in ACTION_SECTIONS[kind]]
