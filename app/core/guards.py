"""Детерминированные проверки результата модели: выполняются кодом до показа менеджеру."""

import re
from dataclasses import dataclass
from itertools import combinations, combinations_with_replacement

from app.core.money import AMOUNT_RE, extract_amounts, group_digits
from app.core.schemas import GuardWarning, Mode, Suggestion, SuggestRequest
from app.kb.models import KnowledgeBase

MAX_REPLY_CHARS = 1000
MAX_UNITS = 3  # сколько штук одной позиции считаем правдоподобным заказом
PRICE_TOLERANCE = 1  # ₽: погрешность округления скидок
MIN_FLAT_FEE = 100  # суммы из текстов БЗ меньше этой — тарифы («50 ₽ за км»), а не слагаемые заказа
MAX_MENTIONED = 8  # сколько названных в тексте сумм перебирать как слагаемые итога
MAX_TRUST_ROUNDS = (
    5  # сколько раз пересчитывать слагаемые без ошибочных: цепочка «цена → итог → итог с доставкой»
)

_CACHE_KEY = "guards_amount_rules"
_DISTINCTIVE_KEY = "guards_distinctive_stems"
_TERMS_KEY = "guards_terms"


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


def _conversation_amounts(request: SuggestRequest) -> tuple[set[int], set[int]]:
    """(суммы менеджера и бота, суммы клиента). Первые ответ может повторять и складывать, вторые — нет:
    иначе «сделайте за 15 000 ₽» → «договорились, 15 000 ₽» проходит проверку цен."""
    messages = (*request.history, *request.replies)
    staff_texts = [m.text for m in messages if m.role != "client"]
    client_texts = [request.message] + [m.text for m in messages if m.role == "client"]

    def amounts(texts: list[str]) -> set[int]:
        return {round(value) for text in texts for _, value in extract_amounts(text)}

    return amounts(staff_texts), amounts(client_texts)


def _unexplained(
    checked: list[tuple[str, float]],
    context: list[tuple[str, float]],
    rules: AmountRules,
    conversation: set[int],
) -> list[tuple[str, float]]:
    """Суммы из checked, которые не выводятся из БЗ и переписки. context — все суммы текста: они годятся
    в слагаемые итога, но только сами объяснённые — итог от ошибочной цены тоже ошибочен."""
    trusted = {round(value) for _, value in context}
    for _ in range(MAX_TRUST_ROUNDS):
        bad = {value for value in trusted if not is_amount_allowed(value, rules, conversation, trusted)}
        if not bad:
            break
        trusted -= bad
    return [
        (raw, value) for raw, value in checked if not is_amount_allowed(value, rules, conversation, trusted)
    ]


# ---------- Скидки в процентах и «бесплатно» ----------

_SENTENCE_RE = re.compile(r"(?:[^.!?\n]|(?<=\d)\.(?=\d))+")
_NEGATION_RE = re.compile(r"\b(?:не|нет|ни|нельзя|невозможно)\b", re.IGNORECASE)
_PERCENT_RE = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:[.,]\d{1,2})?)(?:\s?[–—-]\s?(\d{1,3}(?:[.,]\d{1,2})?))?\s?(?:%|процент)",
    re.IGNORECASE,
)
_FREE_RE = re.compile(r"бесплатн\w*|даром|в\s+подарок|подар(?:им|ю|ят)\b|за\s+наш\s+сч[её]т", re.IGNORECASE)
_CLAUSE_RE = re.compile(r"[,;:]|\s[—–-]\s")
# Слово после этих предлогов — условие, а не то, что бесплатно: «доставка при заказе с монтажом бесплатная».
_CONDITION_WORDS = frozenset({"с", "со", "при", "после", "для", "без", "вместе"})
# Служебные слова не говорят, что именно бесплатно; слова короче четырёх букв отбрасываются и так.
_STOP_WORDS = frozenset(
    {
        "если", "после", "этот", "этом", "этой", "этого", "также", "тоже", "будет", "будут", "можем", "можно",
        "всего", "всех", "очень", "совершенно", "абсолютно", "полностью", "сейчас", "только", "когда",
        "ваша", "ваше", "ваши", "вашего", "вашей", "наша", "наше", "наши", "нашего",
    }
)  # fmt: skip


@dataclass(frozen=True)
class _FreeFact:
    """Часть предложения БЗ, где что-то названо бесплатным: все её слова и слова не из условий."""

    words: frozenset[str]
    subject: frozenset[str]


@dataclass(frozen=True)
class _Terms:
    percents: frozenset[float]
    free: tuple[_FreeFact, ...]
    product_stems: frozenset[str]  # слова, по которым узнаётся платный товар из БЗ


def _content_stems(text: str) -> list[str]:
    words = _WORD_RE.findall(_FREE_RE.sub(" ", text).lower().replace("ё", "е"))
    return [w[:5] for w in words if len(w) >= 4 and w not in _STOP_WORDS]


def _subject_stems(text: str) -> set[str]:
    """Основы слов без тех, что стоят сразу после предлога условия («с монтажом», «при заказе»)."""
    words = _WORD_RE.findall(_FREE_RE.sub(" ", text).lower().replace("ё", "е"))
    return {
        w[:5]
        for i, w in enumerate(words)
        if len(w) >= 4 and w not in _STOP_WORDS and (i == 0 or words[i - 1] not in _CONDITION_WORDS)
    }


def _percents(text: str) -> list[tuple[str, float]]:
    found = []
    for match in _PERCENT_RE.finditer(text):
        for number in (match.group(1), match.group(2)):
            if number:
                found.append((match.group(0), float(number.replace(",", "."))))
    return found


def _ends_sentence(text: str, pos: int) -> bool:
    rest = text[pos:].lstrip(" \t  ")
    return not rest or rest[0] == "\n" or rest[0].isupper()


def _mask_amount_dots(match: re.Match[str]) -> str:
    """Точки внутри суммы («12 тыс. руб.») — не конец предложения; точка в конце суммы («30 000 р.») — конец,
    только если дальше заглавная буква, перенос строки или конец текста: «30 000 руб. не действует» — одно
    предложение. Длина не меняется, чтобы позиции совпадали с исходным текстом."""
    raw = match.group(0)
    last = raw[-1]
    if last == "." and not _ends_sentence(match.string, match.end()):
        last = " "
    return raw[:-1].replace(".", " ") + last


def _sentences(text: str) -> list[str]:
    """Предложения текста вместе с завершающим знаком; сокращения в суммах предложение не обрывают."""
    masked = AMOUNT_RE.sub(_mask_amount_dots, text)
    return [text[m.start() : m.end() + 1] for m in _SENTENCE_RE.finditer(masked)]


def _affirmed(text: str) -> list[str]:
    """Предложения без отрицания: в них ответ утверждает, а не отказывает («скидки 90% нет» — отказ)."""
    return [s for s in _sentences(text) if not _NEGATION_RE.search(s)]


# Сумма клиента как бюджет или верхняя граница: «бюджет 40 000 ₽», «до 40 000 ₽», «в пределах 40 000 ₽».
_BUDGET_RE = re.compile(
    r"бюджет|в\s+пределах|не\s+(?:больше|дороже|выше|более)|максимум|уложи|\bдо\s+\d", re.IGNORECASE
)


def _client_budgets(request: SuggestRequest) -> set[int]:
    """Суммы, которые клиент назвал бюджетом или верхней границей цены."""
    texts = [request.message] + [m.text for m in (*request.history, *request.replies) if m.role == "client"]
    return {
        round(value)
        for text in texts
        for sentence in _sentences(text)
        if _BUDGET_RE.search(sentence)
        for _, value in extract_amounts(sentence)
    }


def _kb_terms(kb: KnowledgeBase) -> _Terms:
    cached = kb._cache.get(_TERMS_KEY)
    if cached is not None:
        return cached
    texts = [kb.company_text]
    texts += [f"{p.name}. {p.description}. {p.for_whom}" for p in kb.products]
    texts += [f.answer for f in kb.faq]
    texts += [p.text for p in kb.policies]
    texts += [f"{r.when}. {r.argument}. {r.discount_condition or ''}. {r.not_when}" for r in kb.upsell_rules]
    percents = {value for text in texts for _, value in _percents(text)}
    percents |= {float(r.discount_percent) for r in kb.upsell_rules if r.discount_percent}
    free = [
        _FreeFact(frozenset(_content_stems(clause)), frozenset(_subject_stems(clause)))
        for text in texts
        for sentence in _SENTENCE_RE.findall(text)
        for clause in _CLAUSE_RE.split(sentence)
        if _FREE_RE.search(clause)
    ]
    # Платные товары узнаём по словам, которые есть в названии только одного товара: «годовое», «монтаж»,
    # «модуль». Общие слова («кондиционер», «сплит-система») ничего не уточняют.
    counts: dict[str, int] = {}
    for product in kb.products:
        for stem in set(_content_stems(product.name)):
            counts[stem] = counts.get(stem, 0) + 1
    products = frozenset(stem for stem, count in counts.items() if count == 1)
    terms = _Terms(frozenset(percents), tuple(free), products)
    kb._cache[_TERMS_KEY] = terms
    return terms


def _free_supported(sentence: str, clause: str, terms: _Terms) -> bool:
    """«Бесплатно» подтверждено, если предложение совпадает с утверждением БЗ хотя бы двумя словами, а платный
    товар, названный не в условии, БЗ тоже называет бесплатным. «При заказе с монтажом доставка бесплатная» —
    да; «монтаж для вас бесплатный», «годовое обслуживание бесплатно при заказе с монтажом» — нет."""
    words = set(_content_stems(sentence))
    named = _subject_stems(clause) & terms.product_stems
    return any(len(words & fact.words) >= 2 and named <= fact.subject for fact in terms.free)


def _unsupported_terms(text: str, terms: _Terms, staff_percents: set[float]) -> list[tuple[str, str]]:
    """Проценты и «бесплатно», которых нет в БЗ и у менеджера: [(вид, как написано)].

    Смотрим только предложения без отрицания. Отрицание про другое в том же предложении проверку обманет —
    она страхует менеджера, а не заменяет его.
    """
    found: list[tuple[str, str]] = []
    allowed = terms.percents | staff_percents
    for sentence in _affirmed(text):
        for raw, value in _percents(sentence):
            if value not in allowed and ("percent", raw) not in found:
                found.append(("percent", raw))
        for clause in _CLAUSE_RE.split(sentence):
            if _FREE_RE.search(clause) and not _free_supported(sentence, clause, terms):
                found.append(("free", sentence.strip()[:80]))
                break
    return found


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


def _terms_message(kind: str, raw: str, where: str) -> str:
    if kind == "percent":
        return f"Процент «{raw}» в {where} не из базы знаний — проверьте, не обещана ли скидка, которой нет."
    return f"«{raw}» — в {where} что-то обещано бесплатно, а база знаний этого не подтверждает."


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

    # Суммы: только те, что выводятся из БЗ или их назвал менеджер.
    rules = amount_rules(kb)
    staff, client = _conversation_amounts(request)
    reply_amounts = extract_amounts(result.client_reply)
    upsell_amounts = extract_amounts(f"{upsell.offer}\n{upsell.pitch}")
    reply_unexplained = _unexplained(reply_amounts, reply_amounts, rules, staff)
    unexplained = {round(value) for _, value in reply_unexplained}
    budgets = _client_budgets(request)
    confirmed: set[int] = set()  # суммы, которые ответ утверждает как цену
    for sentence in _affirmed(result.client_reply):
        values = {round(value) for _, value in extract_amounts(sentence)}
        prices = values - unexplained  # суммы предложения, которые выводятся из БЗ или слов менеджера
        for value in values & unexplained:
            # «В бюджет 40 000 ₽ укладывается Basic 07 с монтажом — 37 800 ₽»: бюджет клиента рядом
            # с ценой из БЗ, которая в него укладывается, — сравнение. «Договорились: 15 000 ₽ вместо
            # 32 900 ₽» — нет: 15 000 клиент назвал не бюджетом, да и цена в него не укладывается.
            if _near(value, budgets) and any(price <= value for price in prices):
                continue
            confirmed.add(value)
    for raw, value in reply_unexplained:
        if _near(value, client):
            if round(value) not in confirmed:
                continue  # «цена 1 рубль не действует» — отказ; бюджет рядом с подходящей ценой — сравнение
            warn(
                "client_amount",
                f"Сумму «{raw}» назвал клиент, а в базе знаний её нет — проверьте, что ответ "
                "не подтверждает её как цену.",
            )
            _add_needs_human(result, "Проверьте суммы, которые назвал клиент.")
        else:
            warn(
                "price_not_in_kb",
                f"Сумма «{raw}» в ответе клиенту не выводится из базы знаний — проверьте цену.",
            )
            _add_needs_human(result, "Проверьте цены в ответе.")
    for raw, _ in _unexplained(upsell_amounts, reply_amounts + upsell_amounts, rules, staff):
        warn(
            "upsell_price_not_in_kb",
            f"Сумма «{raw}» в подсказке по допродаже не выводится из базы знаний — проверьте цену.",
        )

    # Скидки в процентах и «бесплатно»: на них давит prompt-injection, а суммой с валютой они не являются.
    terms = _kb_terms(kb)
    staff_percents = {
        value
        for m in (*request.history, *request.replies)
        if m.role != "client"
        for _, value in _percents(m.text)
    }
    for kind, raw in _unsupported_terms(result.client_reply, terms, staff_percents):
        warn("terms_not_in_kb", _terms_message(kind, raw, "ответе клиенту"))
        _add_needs_human(result, "Проверьте скидки и условия в ответе.")
    for kind, raw in _unsupported_terms(upsell.pitch, terms, staff_percents):
        warn("upsell_terms_not_in_kb", _terms_message(kind, raw, "фразе допродажи"))

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
