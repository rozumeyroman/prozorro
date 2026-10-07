import pytest
from conftest import NOW
from fake_prozorro import make_tender
from test_sync import run_sync

from prozorro_mcp.db import Database
from prozorro_mcp.profiles import FilterError, FilterRegistry, ensure_matches
from prozorro_mcp.settings import DEFAULT_FILTERS_DIR


def registry(tmp_path) -> FilterRegistry:
    return FilterRegistry(DEFAULT_FILTERS_DIR, tmp_path / "Фільтри", "cybersecurity")


def by_title(tenders, title):
    return next(t for t in tenders if t["title"] == title)


def test_cyber_filter_on_demo_data(cyber_filter, tenders):
    d = cyber_filter.evaluate(by_title(tenders, "Антивірусний захист"))
    assert d.relevant and d.topics == ["endpoint"]
    d = cyber_filter.evaluate(by_title(tenders, "Міжмережевий екран"))
    assert not d.relevant and d.stage == "value" and d.topics == ["security_software"]
    for title in ("Закупівля комутаторів для ЦОД", "Серверне обладнання"):
        assert cyber_filter.evaluate(by_title(tenders, title)).stage == "topic"


def test_keyword_groups_become_topics(cyber_filter):
    t = make_tender(
        "Послуги SOC",
        [("72000000-5", "Послуги моніторингу подій інформаційної безпеки (SIEM)")],
        1_000_000,
        created=NOW,
    )
    d = cyber_filter.evaluate(t)
    assert d.relevant and d.topics == ["siem_soc"]
    t = make_tender("ПЗ", [("48000000-8", "Ліцензія CyberArk Privileged Access Manager")], 900_000, created=NOW)
    assert cyber_filter.evaluate(t).topics == ["vendors"]


def test_title_context_single_item(cyber_filter):
    t = make_tender(
        "Антивірус та офісне ПЗ",
        [("48000000-8", "Microsoft Office 2024"), ("48760000-3", "ESET PROTECT Entry")],
        900_000,
        created=NOW,
    )
    d = cyber_filter.evaluate(t)
    assert [m.description for m in d.matches] == ["ESET PROTECT Entry"]
    # with a single item the tender title does count
    t = make_tender(
        "Антивірусне програмне забезпечення", [("48000000-8", "Програмне забезпечення")], 900_000, created=NOW
    )
    assert cyber_filter.evaluate(t).relevant


def test_exclude_keywords_physical_security(cyber_filter):
    t = make_tender(
        "Консультації",
        [("79417000-0", "Консультації з питань охорони об'єкта та відеоспостереження")],
        900_000,
        created=NOW,
    )
    assert not cyber_filter.evaluate(t).relevant


def test_active_filter_persists(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    reg = registry(tmp_path)
    assert reg.active_name(db) == "cybersecurity"
    reg.set_active(db, "it-infrastructure")
    assert registry(tmp_path).active_name(Database(tmp_path / "db.sqlite")) == "it-infrastructure"
    with pytest.raises(FilterError):
        reg.set_active(db, "nope")


def test_save_filter(tmp_path, tenders):
    db = Database(":memory:")
    reg = registry(tmp_path)
    f, path = reg.save("cyber-300k", "cybersecurity", {"min_value": 300000})
    assert path.parent == tmp_path / "Фільтри" and f.min_amount == 300000
    assert f.evaluate(by_title(tenders, "Міжмережевий екран")).relevant
    # edit own profile without base
    f, _ = reg.save(
        "cyber-300k",
        None,
        {"add_keywords": [r"\bMikroTik\b"], "keyword_group": "network_hw", "add_cpv_weak": ["32420000-3"]},
    )
    d = f.evaluate(make_tender("Мережа", [("32420000-3", "Маршрутизатор MikroTik CCR2116")], 900_000, created=NOW))
    assert d.relevant and d.topics == ["network_hw"]
    with pytest.raises(FilterError):
        reg.save("cybersecurity", "cybersecurity", {"min_value": 1})  # built-in names are protected
    with pytest.raises(FilterError):
        reg.save("bad", "cybersecurity", {"add_cpv": ["123"]})
    reg.delete(db, "cyber-300k")
    assert "cyber-300k" not in reg.paths()


def test_exclude_cpv(tmp_path):
    reg = registry(tmp_path)
    f, _ = reg.save("infra-no-cables", "it-infrastructure", {"exclude_cpv": ["32421000-0"]})
    t = make_tender("Мережа", [("32421000-0", "Кабель UTP cat.6")], 900_000, created=NOW)
    assert not f.evaluate(t).relevant
    assert reg.load("it-infrastructure").evaluate(t).relevant


async def test_profiles_have_separate_decisions_and_matches(fake, tender_filter, cyber_filter):
    db = Database(":memory:")
    await run_sync(fake, tender_filter, db)
    # re-evaluating stored tenders under another profile needs no network
    assert ensure_matches(db, cyber_filter) == 1
    assert db.search(profile="cybersecurity")[1] == 1
    assert db.search(profile="it-infrastructure")[1] == 3
    # decisions are per profile: a cyber sync evaluates tenders the broad profile already decided
    stats = await run_sync(fake, cyber_filter, db)
    assert stats["fetched_new"] == 6 and stats["relevant_found"] == 1
