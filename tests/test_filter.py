from conftest import NOW
from fake_prozorro import make_tender

from prozorro_mcp.filter import cpv_prefix


def by_title(tenders, title):
    return next(t for t in tenders if t["title"] == title)


def test_cpv_prefix():
    assert cpv_prefix("32420000-3") == "3242"
    assert cpv_prefix("48820000-2") == "4882"
    assert cpv_prefix("30233141-1") == "30233141"


def test_strong_cpv_relevant(tender_filter, tenders):
    d = tender_filter.evaluate(by_title(tenders, "Закупівля комутаторів для ЦОД"))
    assert d.relevant and d.topics == ["network"]


def test_weak_cpv_needs_keyword(tender_filter, tenders):
    d = tender_filter.evaluate(by_title(tenders, "Серверне обладнання"))
    assert d.relevant
    # the monitor matches only through the tender/lot title ("Серверне обладнання"/"Сервери")
    assert {m.topic for m in d.matches} == {"keyword"}
    d = tender_filter.evaluate(by_title(tenders, "Закупівля картриджів"))
    assert not d.relevant and d.stage == "topic"


def test_exclude_keyword_beats_context():
    from prozorro_mcp.filter import TenderFilter
    from prozorro_mcp.settings import DEFAULT_FILTER_CONFIG

    f = TenderFilter.from_file(DEFAULT_FILTER_CONFIG)
    t = make_tender(
        "Серверне обладнання та витратні матеріали", [("30200000-1", "Картридж HP 59A")], 900_000, created=NOW
    )
    assert not f.evaluate(t).relevant


def test_value_threshold(tender_filter, tenders):
    d = tender_filter.evaluate(by_title(tenders, "Міжмережевий екран"))
    assert not d.relevant and d.stage == "value" and d.topics == ["cybersecurity"]


def test_value_counts_only_relevant_lots(tender_filter, tenders):
    t = by_title(tenders, "Меблі та мережеве обладнання")
    d = tender_filter.evaluate(t)
    assert not d.relevant and d.relevant_value == 300_000
    tender_filter.value_scope = "tender"
    assert tender_filter.evaluate(t).relevant


def test_prefilter(tender_filter, tenders):
    assert tender_filter.prefilter(by_title(tenders, "Прямий договір: маршрутизатори"))
    assert tender_filter.prefilter(by_title(tenders, "Спрощена закупівля: мережеве обладнання"))
    assert tender_filter.prefilter(by_title(tenders, "Антивірусний захист")) is None  # special entity
    assert "сума лотів" in tender_filter.prefilter(by_title(tenders, "Кабельна продукція")).reason
    assert tender_filter.prefilter(by_title(tenders, "Закупівля комутаторів для ЦОД")) is None
