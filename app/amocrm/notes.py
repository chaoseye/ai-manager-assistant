"""Тексты служебных примечаний в ленте сделки. Подпись сервиса (NOTE_SERVICE_NAME) amoCRM показывает сама."""

from app.core.schemas import SuggestResult

TIMING_LABELS = {
    "now": "предложить сейчас",
    "after_resolution": "после решения вопроса клиента",
    "not_now": "не предлагать сейчас",
}
SEPARATOR = "────────"


def format_suggestion_note(result: SuggestResult, *, include_reply: bool) -> str:
    s, u = result.suggestion, result.suggestion.upsell
    lines: list[str] = []
    if include_reply:
        lines += ["Черновик ответа клиенту:", s.client_reply]
        if s.kb_refs:
            lines.append("Основано на: " + ", ".join(s.kb_refs))
        if s.needs_human:
            lines.append(f"⚠ Проверьте перед отправкой: {s.needs_human_reason or 'нужна проверка менеджера'}")
    else:
        lines.append("Менеджер уже ответил клиенту — черновик не нужен.")

    timing = TIMING_LABELS[u.timing] if u.recommended else "не предлагать"
    lines += ["", SEPARATOR, f"Допродажа (только для менеджера) · {timing}"]
    if u.recommended:
        lines.append(f"Что: {u.offer}")
        lines.append(f"Почему: {u.reason}")
        if u.pitch:
            lines.append(f"Фраза: «{u.pitch}»")
    else:
        lines.append(f"Почему нет: {u.reason}")
    if u.avoid:
        lines.append(f"Не делать: {u.avoid}")

    if result.meta.warnings:
        lines += ["", "Предупреждения проверок:"]
        lines += [f"- {w.message}" for w in result.meta.warnings]
    return "\n".join(lines)


def format_attachment_note(attachment_types: list[str]) -> str:
    kinds = ", ".join(sorted({t for t in attachment_types if t})) or "файл"
    return (
        f"Клиент прислал вложение без текста ({kinds}). Распознавание вложений не поддерживается — "
        "ответьте клиенту самостоятельно."
    )


def format_failure_note(reason: str) -> str:
    return f"Подсказка не сформирована: {reason}. Ответьте клиенту самостоятельно."
