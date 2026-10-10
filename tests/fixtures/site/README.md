# Реальні відповіді prozorro.gov.ua і public-api (знято 10.10.2026)

Фікстури для тестів пошуку внутрішнього id тендера за номером UA-…. Знято в хмарі через curl, без змін. Відкрити ці адреси з пісочниці Claude Code не можна.

| Файл | Запит | Відповідь |
|---|---|---|
| `site_tender_page_UA-2025-01-03-000122-a.html` | `GET https://prozorro.gov.ua/tender/UA-2025-01-03-000122-a` | 200, 842 байти: сторінка без даних, у ній немає ні id, ні тендера. **Тому поточний `page_candidates()` нічого не знаходить.** |
| `site_summary_UA-2025-01-03-000122-a.json` | `GET https://prozorro.gov.ua/api/tenders/UA-2025-01-03-000122-a/summary` | 200 JSON, плоский об'єкт (без обгортки `data`): `id` = `8d122ba0e00e4b8b9a081913bc71f37c`, `tenderID`, `dateModified`, `status`, `value`, `tenderPeriod`, `procuringEntity`… **Цей запит робить сам сайт, коли відкривається сторінка тендера.** |
| `site_summary_UA-2025-01-08-005675-a.json` | те саме для `UA-2025-01-08-005675-a` | 200, `id` = `d900575e21944283a22bd868b858c496` |
| `site_details_UA-2025-01-03-000122-a.json` | `GET …/api/tenders/UA-…/details` | 200, ~84 КБ: awards, numberOfBids тощо. Для пошуку id не потрібен, доданий для повноти. |
| `site_summary_by_internal_id_8d122….json` | `GET …/api/tenders/8d122ba0…/summary` (внутрішній id) | 200 `{"tenderID": "UA-2025-01-03-000122-a"}`: зворотний напрямок, від id до UA. |
| `site_summary_404_UA-2099-01-01-000001-a.json` + `_404_headers.txt` | неіснуючий UA | 404 `{"message": ""}` |
| `site_summary_headers.txt` | заголовки 200-відповіді | `content-type: application/json`, **`x-ratelimit-limit: 60`** (на хвилину), `x-ratelimit-remaining` |
| `site_search_cpv48760000-3_2025-01_p1.json` | `POST https://prozorro.gov.ua/api/search/tenders`, form: `cpv[]=48760000-3&date[tender][start]=2025-01-01&date[tender][end]=2025-01-31&page=1` | 200 `{page, per_page, total, data[]}`; у `data` немає внутрішнього `id`, є лише `tenderID` |
| `site_search_text_UA-2025-01-03-000122-a.json` | той самий POST з `text=UA-2025-01-03-000122-a` | 200, 1 результат, теж без `id` |
| `public_api_tender_8d122ba0e00e4b8b9a081913bc71f37c.json` | `GET https://public-api.prozorro.gov.ua/api/2.5/tenders/8d122ba0…` | 200 `{"data": {...}}`, повний тендер (FortiSIEM, Львів) |
| `public_api_tender_d900575e21944283a22bd868b858c496.json` | те саме для `d900575e…` | 200, повний тендер (Черкасиобленерго) |

Обидва тендери `coverage --from 2025-01-01 --to 2025-01-31` позначив як «немає в базі; внутрішній id не знайдено», хоча в базі їх справді немає.

Додатково перевірено:
- `public-api …/tenders?tenderID=UA-…` фільтр **ігнорує** і повертає звичайну стрічку. Цей спосіб не підходить.
- 15 запитів summary поспіль пройшли без 429, але сайт оголошує ліміт 60 запитів на хвилину.
- Особливих заголовків запит не потребує: звичайний curl без cookie і без User-Agent отримує 200.
