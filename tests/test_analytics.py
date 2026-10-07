from conftest import NOW
from fake_prozorro import make_tender

from prozorro_mcp.analytics import summarize


def test_summarize(tender_filter, tenders):
    titles = {"Закупівля комутаторів для ЦОД", "Серверне обладнання", "Антивірусний захист"}
    relevant = [t for t in tenders if t["title"] in titles]
    s = summarize(relevant, tender_filter)
    assert s["tenders"] == 3
    assert s["expected_value_uah"] == 1_200_000 + 2_500_000 + 800_000
    assert s["awards"] == 1 and s["awarded_value_uah"] == 984_000
    assert s["top_winners_by_amount"][0]["edrpou"] == "12345678"
    assert s["competition"] == {"awards_with_bids": 1, "avg_bidders": 2, "single_bidder_share": 0.0}
    assert s["discount"] == {"n": 1, "median": 0.18, "mean": 0.18}
    assert s["by_status"]["Завершено"]["count"] == 1
    assert set(s["by_topic"]) == {"network", "keyword", "cybersecurity"}
    assert list(s["by_month"]) == [NOW.strftime("%Y-%m")]


def test_single_bidder_share(tender_filter):
    t = make_tender("Комутатори", [("32420000-3", "Комутатор Cisco")], 1_000_000, created=NOW, with_results=True)
    t["bids"] = t["bids"][:1]
    s = summarize([t], tender_filter)
    assert s["competition"]["single_bidder_share"] == 1.0
