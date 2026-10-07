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
    f, _ = reg.save("infra-no-routers", "it-infrastructure", {"exclude_cpv": ["32413100-2"]})
    t = make_tender("Мережа", [("32413100-2", "Маршрутизатор Juniper MX204")], 900_000, created=NOW)
    assert not f.evaluate(t).relevant
    assert reg.load("it-infrastructure").evaluate(t).relevant


@pytest.mark.parametrize(
    ("cpv", "description"),
    [
        ("32421000-0", "Комутатор"),  # cables code is excluded whatever the description says
        ("32581200-1", "Факс Panasonic"),
        ("30236110-6", "Сервер"),  # RAM code
        ("30237135-4", "Мережевий адаптер Intel X710"),
        ("32420000-3", "Кабель UTP cat.6, 305 м"),  # cable filed under the parent network code
        ("32420000-3", "Патч-корд RJ45 1 м"),
        ("30230000-0", "Модуль пам'яті DDR5 64GB для сервера Dell"),
        ("30230000-0", "Жорсткий диск 2.4TB SAS для СХД"),
        ("48820000-2", "Блок живлення для сервера HPE"),
        ("30230000-0", "Шафа серверна 42U"),
    ],
)
def test_default_exclusions(tender_filter, cpv, description):
    t = make_tender("Серверне та мережеве обладнання", [(cpv, description)], 900_000, created=NOW)
    assert not tender_filter.evaluate(t).relevant


@pytest.mark.parametrize(
    ("cpv", "description"),
    [
        ("32420000-3", "Комутатор Cisco C1300-48T з кабелем живлення"),
        ("30230000-0", "Сервер Dell PowerEdge R760 (2x CPU, 512GB RAM)"),
        ("48820000-2", "Сервер HPE ProLiant DL380 Gen11"),
    ],
)
def test_main_products_still_pass(tender_filter, cpv, description):
    t = make_tender("Обладнання", [(cpv, description)], 900_000, created=NOW)
    assert tender_filter.evaluate(t).relevant


def test_cyber_excludes_parts(cyber_filter):
    t = make_tender("Fortinet", [("32420000-3", "Блок живлення для FortiGate 200F")], 900_000, created=NOW)
    assert not cyber_filter.evaluate(t).relevant
    t = make_tender("Fortinet", [("32420000-3", "Міжмережевий екран FortiGate 200F")], 900_000, created=NOW)
    assert cyber_filter.evaluate(t).relevant


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


CYBER_POSITIVE = [
    ("48000000-8", "Ліцензія FortiGate-100F Unified Threat Protection на 1 рік"),
    ("48000000-8", "Програмне забезпечення для захисту кінцевих точок, 250 ліцензій"),
    ("72000000-5", "Послуги з технічної підтримки системи SIEM"),
    ("72000000-5", "Продовження підписки на систему захисту електронної пошти"),
    ("48000000-8", "Сканер вразливостей Nessus Professional"),
    ("72000000-5", "Послуги з проведення тестування на проникнення"),
    ("48000000-8", "Система управління привілейованим доступом"),
    ("32420000-3", "Апаратний міжмережевий екран"),
    ("72000000-5", "Послуги з побудови КСЗІ"),
    ("48000000-8", "Система виявлення та запобігання вторгненням"),
    ("72000000-5", "Послуги захисту від DDoS-атак"),
    ("48000000-8", "Засіб захисту від шкідливого програмного забезпечення"),
    ("48000000-8", "Система керування подіями інформаційної безпеки"),
    ("48000000-8", "Kaspersky Endpoint Security for Business"),
    ("48000000-8", "Microsoft 365 E5 Security"),
    ("48000000-8", "Платформа Breach and Attack Simulation"),
    ("32420000-3", "Система IPS/IDS з підпискою на сигнатури"),
    ("32420000-3", "Шлюз безпеки з функціями IPS, 10 Гбіт/с"),
    ("72000000-5", "Послуги цілодобового моніторингу безпеки та реагування на інциденти"),
    ("72000000-5", "Підписка на сервіс threat intelligence"),
    ("72000000-5", "Послуги із захисту вебресурсів"),
    ("48000000-8", "Платформа security awareness з фішинг-симуляціями"),
    ("48000000-8", "Програмна продукція для пристрою Cisco FPR4110"),
    ("72000000-5", "Послуги з постачання засобів криптографічного захисту інформації"),
]
CYBER_NEGATIVE = [
    ("72000000-5", "Технічне обслуговування системи BAS (автоматизація будівлі)"),
    ("48000000-8", "Microsoft Windows Server 2022 Standard"),
    ("48000000-8", "Microsoft Office 2024"),
    ("79417000-0", "Консультації з питань охорони об'єкта"),
    ("30230000-0", "Ноутбук Dell Latitude з антивірусом"),
    ("72000000-5", "Послуги з розробки веб-сайту"),
    # калібрування III кв. 2026: IPS-матриця дисплея
    ("30230000-0", 'Універсальний ПК (23.8", IPS, 16GB, 512GB)'),
    ("30230000-0", 'Дисплей: LG/ASUS 31.5";IPS;3840x2160;16:9'),
    ("30230000-0", "Монітор 27 IPS-матриця 2560x1440"),
    ("30230000-0", "Зовнішній SSD накопичувач з апаратним шифруванням 8 ТБ"),
    ("48510000-6", "Ліцензія на активацію алгоритму шифрування AES 256 (для радіостанцій Motorola)"),
]


@pytest.mark.parametrize(("cpv", "description"), CYBER_POSITIVE)
def test_cyber_positive_phrases(cyber_filter, cpv, description):
    t = make_tender("Закупівля", [(cpv, description), ("39100000-3", "Стіл")], 900_000, created=NOW)
    assert cyber_filter.evaluate(t).relevant, description


@pytest.mark.parametrize(("cpv", "description"), CYBER_NEGATIVE)
def test_cyber_negative_phrases(cyber_filter, cpv, description):
    t = make_tender("Закупівля", [(cpv, description), ("39100000-3", "Стіл")], 900_000, created=NOW)
    assert not cyber_filter.evaluate(t).relevant, description


def test_context_match_shows_snippet(cyber_filter):
    t = make_tender(
        "Ліцензії Red Hat з урахуванням вимог щодо захисту інформації в ІКС",
        [("48000000-8", "Red Hat OpenShift")],
        900_000,
        created=NOW,
    )
    item, context = cyber_filter.item_contexts(t)[0]
    tr = cyber_filter.trace_item(item, context)
    assert tr.rule == "weak_keyword" and "вимог щодо захисту інформації" in tr.detail
