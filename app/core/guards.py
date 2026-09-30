"""Детерминированные проверки результата модели: выполняются кодом до показа менеджеру."""

import re
from dataclasses import dataclass
from itertools import combinations, combinations_with_replacement

from app.core.money import extract_amounts, group_digits
from app.core.schemas import GuardWarning, Mode, Suggestion, SuggestRequest
from app.kb.models import KnowledgeBase

MAX_REPLY_CHARS = 1000
MAX_UNITS = 3  # сколько штук одной позиции считаем правдоподобным заказом
PRICE_TOLERANCE = 1  # ₽: погрешность округления скидок
MIN_FLAT_FEE = 100  # суммы из текстов БЗ меньше этой — тарифы («50 ₽ за км»), а не слагаемые заказа
MAX_MENTIONED = 8  # сколько названных в тексте сумм перебирать как слагаемые итога

_CACHE_KEY = "guards_amount_rules"
_DISTINCTIVE_KEY = "guards_distinctive_stems"


@dataclass(frozen=True)
class AmountRules:
    """Какие суммы выводятся из базы знаний.

    atomic      — цены, суммы из текстов БЗ, цены со скидкой, размер скидки, 2–3 штуки одной позиции;
    components  — слагаемые заказа: цены, цены со скидкой, фиксированные платежи из текстов (доставка);
    totals      — суммы двух слагаемых и скидки на пару позиций одной категории.
    Длинные итоги («итого 47 520 ₽») разрешены, только если остальные слагаемые названы рядом.
    """

    atomic: frozenset[int]
    components: frozenset[int]
    totals: frozenset[int]
    discount_percents: frozenset[int]


def _kb_text_amounts(kb: KnowledgeBase) -> set[float]:
    texts = [kb.company_text]
    texts += [f.answer for f in kb.faq]
    texts += [p.text for p in kb.policies]
    texts += [f"{r.argument} {r.discount_condition or ''}" for r in kb.upsell_rules]
    return {value for text in texts for _, value in extract_amounts(text)}


def amount_rules(kb: KnowledgeBase) -> AmountRules:
    cached = kb._cache.get(_CACHE_KEY)
    if cached is not None:
        return cached

    prices = [float(p.price) for p in kb.products]
    text_amounts = _kb_text_amounts(kb)
    atomic: set[float] = set(prices) | text_amounts
    atomic |= {price * units for price in prices for units in range(2, MAX_UNITS + 1)}
    components: set[float] = set(prices) | {a for a in text_amounts if a >= MIN_FLAT_FEE}
    totals: set[float] = set()

    for rule in kb.upsell_rules:
        if not rule.discount_percent:
            continue
        keep, cut = (100 - rule.discount_percent) / 100, rule.discount_percent / 100
        for product_id in rule.offer:
            price = float(kb.products_by_id[product_id].price)
            atomic |= {price * keep, price * cut}
            components.add(price * keep)
        # Скидка на заказ из нескольких позиций той же категории («−5% при заказе двух кондиционеров»).
        categories = {kb.products_by_id[pid].category for pid in rule.offer}
        eligible = [float(p.price) for p in kb.products if p.category in categories]
        for first, second in combinations_with_replacement(eligible, 2):
            totals |= {(first + second) * keep, (first + second) * cut}

    totals |= {sum(pair) for pair in combinations_with_replacement(sorted(components), 2)}

    rules = AmountRules(
        atomic=frozenset(round(v) for v in atomic),
        components=frozenset(round(v) for v in components),
        totals=frozenset(round(v) for v in totals),
        discount_percents=frozenset(r.discount_percent for r in kb.upsell_rules if r.discount_percent),
    )
    kb._cache[_CACHE_KEY] = rules
    return rules


def _near(value: float, pool: frozenset[int] | set[int]) -> bool:
    rounded = round(value)
    return any((rounded + delta) in pool for delta in range(-PRICE_TOLERANCE, PRICE_TOLERANCE + 1))


def is_amount_allowed(
    value: float, rules: AmountRules, conversation: set[int], nearby: set[int] | None = None
) -> bool:
    """conversation — суммы из переписки (их назвали клиент или менеджер, повторять можно);
    nearby — другие суммы того же сгенерированного текста (годятся только как слагаемые итога)."""
    if _near(value, rules.atomic) or _near(value, rules.totals) or _near(value, conversation):
        return True
    # Итог из нескольких позиций: часть слагаемых названа рядом, остаток — слагаемое(ые) из БЗ.
    summands = conversation | (nearby or set())
    others = sorted(m for m in summands if abs(m - value) > PRICE_TOLERANCE)[:MAX_MENTIONED]
    pools = (rules.components, rules.totals)
    for size in (1, 2, 3):
        for subset in combinations(others, size):
            subtotal = sum(subset)
            rest = value - subtotal
            if abs(rest) <= PRICE_TOLERANCE or any(_near(rest, pool) for pool in pools):
                return True
            if any(_near(value, {round(subtotal * (100 - p) / 100)}) for p in rules.discount_percents):
                return True
    return False


def _conversation_amounts(request: SuggestRequest) -> set[int]:
    texts = [request.message] + [m.text for m in (*request.history, *request.replies)]
    return {round(value) for text in texts for _, value in extract_amounts(text)}


_WORD_RE = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)
# Внутреннее устройство помощника: клиент о нём не знает, и в ответе ему это звучит странно.
_INTERNAL_RE = re.compile(
    r"баз[аеуыо]й? знаний|промпт|(?:игнорир\w*|мо[иейх]\w*|сво[иейх]\w*|предыдущ\w*|системн\w*)\s+инструкци",
    re.IGNORECASE,
)


def _stems(text: str) -> set[str]:
    """Основы слов (первые 5 букв) — чтобы «очистителя воздуха» совпало с «Очиститель воздуха»."""
    return {w[:5] for w in _WORD_RE.findall(text.lower().replace("ё", "е")) if len(w) >= 3}


def _distinctive_stems(kb: KnowledgeBase) -> dict[str, list[str]]:
    """Для каждого товара — основы слов названия, которых нет в названиях других товаров, по порядку.

    У кондиционеров их нет: названия различаются только цифрами (Basic 07 / 09 / 12), поэтому их
    упоминание по названию не отличить от упоминания основного товара — такие товары не проверяем.
    """
    cached = kb._cache.get(_DISTINCTIVE_KEY)
    if cached is not None:
        return cached
    ordered = {
        p.id: list(
            dict.fromkeys(w[:5] for w in _WORD_RE.findall(p.name.lower().replace("ё", "е")) if len(w) >= 3)
        )
        for p in kb.products
    }
    counts: dict[str, int] = {}
    for stems in ordered.values():
        for stem in stems:
            counts[stem] = counts.get(stem, 0) + 1
    result = {pid: [stem for stem in stems if counts[stem] == 1] for pid, stems in ordered.items()}
    kb._cache[_DISTINCTIVE_KEY] = result
    return result


def _named_in(text_stems: set[str], distinctive: list[str], *, loose: bool = False) -> bool:
    """Товар назван в тексте: есть первое отличительное слово названия и всего совпало хотя бы два
    (loose — достаточно первого: клиент пишет коротко, «а очиститель у вас есть?»)."""
    if not distinctive or distinctive[0] not in text_stems:
        return False
    return loose or len(set(distinctive) & text_stems) >= min(2, len(distinctive))


def _add_needs_human(suggestion: Suggestion, reason: str) -> None:
    suggestion.needs_human = True
    current = suggestion.needs_human_reason.strip()
    if reason not in current:
        suggestion.needs_human_reason = f"{current} {reason}".strip() if current else reason


def apply_guards(
    suggestion: Suggestion,
    kb: KnowledgeBase,
    request: SuggestRequest,
    mode: Mode,
    *,
    upsell_in_reply: bool = False,
) -> tuple[Suggestion, list[GuardWarning]]:
    """Возвращает исправленную копию результата и список предупреждений.
    upsell_in_reply — разрешено ли предлагать допродажу прямо в ответе клиенту (UPSELL_IN_REPLY)."""
    result = suggestion.model_copy(deep=True)
    warnings: list[GuardWarning] = []

    def warn(code: str, message: str) -> None:
        warnings.append(GuardWarning(code=code, message=message))

    if mode == "upsell_only":
        result.client_reply = ""
        result.kb_refs = []
    # Формат сумм «9 900 ₽» — модели иногда пишут «9900 ₽». Значение не меняется, только запись.
    result.client_reply = group_digits(result.client_reply)
    result.upsell.pitch = group_digits(result.upsell.pitch)
    result.upsell.offer = group_digits(result.upsell.offer)

    # Ссылки на БЗ.
    valid_refs = [ref for ref in dict.fromkeys(result.kb_refs) if ref in kb.all_ids]
    for ref in dict.fromkeys(result.kb_refs):
        if ref not in kb.all_ids:
            warn("unknown_kb_ref", f"Ссылка на несуществующую запись базы знаний удалена: «{ref}».")
    result.kb_refs = valid_refs
    if mode == "full" and result.answer_found_in_kb and not result.kb_refs:
        warn("no_kb_refs", "Ответ помечен как найденный в базе знаний, но без ссылок на её записи.")

    # Товары в допродаже.
    upsell = result.upsell
    valid_products = [pid for pid in dict.fromkeys(upsell.product_ids) if pid in kb.products_by_id]
    for pid in dict.fromkeys(upsell.product_ids):
        if pid not in kb.products_by_id:
            warn("unknown_product", f"Несуществующий товар удалён из допродажи: «{pid}».")
    upsell.product_ids = valid_products
    if upsell.recommended and not upsell.product_ids:
        upsell.recommended = False
        warn(
            "upsell_without_products",
            "Допродажа рекомендована без товаров из базы знаний — рекомендация снята.",
        )

    # Допродажа в ответе клиенту, хотя решение о ней за менеджером. Товары, которые клиент назвал сам
    # или которые уже есть в сделке, не считаем: о них ответ говорит по делу.
    if not upsell_in_reply and mode == "full":
        distinctive = _distinctive_stems(kb)
        reply_stems = _stems(result.client_reply)
        context_stems = _stems(" ".join([request.message, *(m.text for m in request.history)]))
        in_deal = set(request.lead.products) if request.lead else set()
        for pid in upsell.product_ids:
            product = kb.products_by_id[pid]
            if pid in in_deal or _named_in(context_stems, distinctive[pid], loose=True):
                continue
            if _named_in(reply_stems, distinctive[pid]):
                warn(
                    "upsell_in_reply",
                    f"Товар из допродажи «{product.name}» назван в ответе клиенту — "
                    "предлагать его или нет, решает менеджер.",
                )

    # Допродажа при жалобе или негативе.
    if (result.sentiment == "negative" or result.intent == "complaint") and upsell.timing == "now":
        upsell.timing = "not_now"
        warn("upsell_on_complaint", "Клиент недоволен — допродажа перенесена в «не предлагать сейчас».")

    # Суммы: только те, что выводятся из БЗ или названы в переписке.
    rules = amount_rules(kb)
    conversation = _conversation_amounts(request)
    reply_amounts = extract_amounts(result.client_reply)
    upsell_amounts = extract_amounts(f"{upsell.offer}\n{upsell.pitch}")
    in_reply = {round(v) for _, v in reply_amounts}
    in_upsell = in_reply | {round(v) for _, v in upsell_amounts}
    for raw, value in reply_amounts:
        if not is_amount_allowed(value, rules, conversation, in_reply):
            warn(
                "price_not_in_kb",
                f"Сумма «{raw}» в ответе клиенту не выводится из базы знаний — проверьте цену.",
            )
            _add_needs_human(result, "Проверьте цены в ответе.")
    for raw, value in upsell_amounts:
        if not is_amount_allowed(value, rules, conversation, in_upsell):
            warn(
                "upsell_price_not_in_kb",
                f"Сумма «{raw}» в подсказке по допродаже не выводится из базы знаний — проверьте цену.",
            )

    # Внутренние термины в ответе клиенту.
    internal = _INTERNAL_RE.search(result.client_reply)
    if internal:
        warn(
            "internal_terms",
            f"Ответ клиенту упоминает внутреннее устройство помощника («{internal.group(0)}») — перепишите.",
        )

    # Запрещённые фразы.
    for label, text in (("ответе клиенту", result.client_reply), ("фразе допродажи", upsell.pitch)):
        lowered = text.lower()
        for phrase in kb.forbidden_phrases:
            if phrase.lower() in lowered:
                warn("forbidden_phrase", f"Запрещённая фраза «{phrase}» в {label}.")

    if len(result.client_reply) > MAX_REPLY_CHARS:
        warn(
            "reply_too_long",
            f"Ответ клиенту длиннее {MAX_REPLY_CHARS} символов ({len(result.client_reply)}).",
        )

    return result, warnings
