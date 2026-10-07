"""A broad net for "smells of information security": used to keep a short digest of every rejected tender
(so calibration can re-check only the risky ones) and to explain possible misses in the calibration report."""

from __future__ import annotations

import re
from typing import Any

PROBE = re.compile(
    r"кібер|інформаційн\w* безпек|захист\w* (інформац|даних|мереж|пошти|від)|безпек\w* (мереж|даних|інформ)|"
    r"антивір|вірус|шифр|криптограф|КСЗІ|ЕЦП|КЕП|\bPKI\b|сертифікат\w* (SSL|TLS)|"
    r"firewall|файрвол|фаєрвол|брандмау|міжмереж|\bVPN\b|proxy|проксі|\bWAF\b|\bIDS\b|\bIPS\b|\bUTM\b|NGFW|"
    r"\bSIEM\b|\bSOC\b|\bEDR\b|\bXDR\b|\bNDR\b|\bMDR\b|\bDLP\b|\bPAM\b|\bIAM\b|\bMFA\b|двофактор|автентифікац|"
    r"пентест|проникнен|вразлив|аудит\w* (безпек|ІТ|інформ)|моніторинг\w* (подій|безпек|інцидент)|інцидент|"
    r"Fortinet|FortiGate|Palo Alto|Check ?Point|Sophos|ESET|Bitdefender|CrowdStrike|SentinelOne|Kaspersky|"
    r"Splunk|QRadar|Wazuh|Tenable|Nessus|Qualys|Rapid7|Imperva|CyberArk|Zscaler|Trend Micro|Trellix|"
    r"Forcepoint|Varonis|Acronis|Veeam|Cisco|Juniper|MikroTik",
    re.I,
)


def probe_hits(texts: list[str]) -> list[str]:
    return sorted({m.group(0).lower() for t in texts for m in PROBE.finditer(t or "")})


def tender_digest(tender: dict[str, Any]) -> dict[str, Any]:
    """What a rejected tender needs to be judged later without fetching it again."""
    texts = [tender.get("title") or "", tender.get("description") or ""]
    texts += [lot.get("title") or "" for lot in tender.get("lots") or []]
    texts += [item.get("description") or "" for item in tender.get("items") or []]
    cpvs = {((i.get("classification") or {}).get("id") or "") for i in tender.get("items") or []}
    return {
        "title": tender.get("title"),
        "cpvs": ",".join(sorted(c for c in cpvs if c)),
        "value": (tender.get("value") or {}).get("amount"),
        "probe": ", ".join(probe_hits(texts)),
    }
