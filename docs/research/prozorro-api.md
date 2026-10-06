# Дослідження: репозиторії ProzorroUKR та API для MCP-сервера

Мета проєкту: MCP-сервер, через який Claude може:

1. знаходити закупівлі з товарами, які продаємо ми;
2. показувати, хто в них переміг (конкуренти) і за якою ціною;
3. аналізувати технічні завдання (ТЗ) і ціни, з якими виграли торги.

Нижче підсумок аналізу https://github.com/ProzorroUKR: що там є, які API доступні публічно і як їх використати.

---

## 1. Головні висновки

- **Основне джерело даних: публічний API ЦБД** (`openprocurement.api`), `https://public-api.prozorro.gov.ua/api/2.5/`. Він віддає тендери, контракти й плани в повному обсязі: позиції (CPV, опис, кількість, одиниці виміру), переможців (ЄДРПОУ, назва, сума), пропозиції учасників з цінами за одиницю, а також посилання на документи, зокрема на ТЗ.
- **У ЦБД API немає пошуку чи фільтрів.** Є лише фід змін (курсорна пагінація за часом модифікації) і отримання об'єкта за `id`. Тому знайти, наприклад, «всі тендери з CPV 33140000, де переміг ЄДРПОУ X», напряму неможливо. **Потрібна власна локальна база (індекс)**, яку наповнює синхронізатор фіду, і MCP-інструменти мають шукати саме по ній.
- **Prozorro Market / Catalog API** (`prozorro-catalog`) уже рахує **ринкові ціни на товари з каталогу**: квартилі цін за одиницю з пропозицій за 7 днів (`/api/products/{id}/prices`). Для товарів, що є в каталозі, це готова аналітика цін.
- **Risks API** (`prozorro-risks`) дає індикатори ризиків по тендеру (правила ДАСУ). Корисно як додатковий контекст, для MVP не обов'язково.
- **Audit API** (`openprocurement.audit.api`) містить моніторинги ДАСУ по тендерах. Це теж додатковий контекст.
- Бібліотека **`standards`** містить довідники CPV (ДК 021), одиниці виміру, коди регіонів тощо. Її можна підключити як pip-пакет для розшифровки кодів і пошуку CPV за назвою.
- **`prozorro_crawler`** це офіційна бібліотека для синхронізації фіду (вперед і назад, збереження позиції в Mongo або Postgres, обробка 429). Її можна використати як основу синхронізатора.

---

## 2. Огляд репозиторіїв

| Репозиторій | Що це | Наскільки корисне нам |
|---|---|---|
| **openprocurement.api** | ЦБД (центральна база даних) Prozorro: тендери, контракти, плани, фреймворки, угоди. Документація в `docs/source` | **Критично**: основне джерело даних, моделі даних, опис фіду |
| **prozorro_crawler** | Бібліотека для читання фіду ЦБД (aiohttp) | **Високо**: готовий синхронізатор |
| **prozorro-catalog** | API Prozorro Market: категорії, профілі, товари, постачальники, оферти, **ціни** | **Високо**: ринкові ціни, характеристики товарів |
| **standards** | Довідники: CPV/ДК021, ДК, КАТОТТГ, валюти, кодлисти | **Високо**: розшифровка кодів, пошук CPV |
| **prozorro-risks** | Розрахунок ризик-індикаторів по тендерах і контрактах | Середньо: додатковий контекст |
| **openprocurement.audit.api** | Моніторинги та інспекції ДАСУ | Середньо: додатковий контекст |
| **openprocurement.client.python** | Старий Python-клієнт API (оновлювався 2024) | Низько: простіше писати свій клієнт на httpx |
| **procedure_tools** | Утиліта для тестування API: створення процедур у sandbox | Низько: приклади запитів |
| **prozorro-pdf**, **prozorro_tasks**, **reports**, **prozorro-eds**, **openprocurement.storage.*** | Внутрішня інфраструктура: генерація PDF, фонові задачі, білінг, КЕП, сховища документів | Не потрібні |
| **prozorro-ui** | Vue 3 дизайн-система | Не потрібна |
| **prozorro-public-mongo** | Внутрішня база для Tableau | Не потрібна (не публічна) |
| bridges, auction, chronograph, robot_tests тощо | Старі внутрішні сервіси | Не потрібні |

---

## 3. Доступні API в деталях

### 3.1. ЦБД, публічний API (основний)

- Прод: `https://public-api.prozorro.gov.ua/api/2.5/`
- Sandbox: `https://public-api-sandbox.prozorro.gov.ua/api/2.5/`
- Читання без авторизації. Під час надто частих запитів сервер повертає **429**; потрібен backoff.
- Документація: https://prozorro-api-docs.readthedocs.io/uk/master/ (джерело: `openprocurement.api/docs/source`).

**Фіди (списки змін):** `/tenders`, `/contracts`, `/plans`, `/frameworks`, `/submissions`, `/qualifications`, `/agreements`.

Параметри фіду (`docs/source/basic-actions/feed.rst`):

| Параметр | Значення |
|---|---|
| `offset` | курсор `{timestamp}.{skip_len}.{skip_hash}`; брати з `next_page.offset` |
| `limit` | 1..1000, за замовчуванням 100 |
| `descending=1` | від нових до старих |
| `mode` | без параметра: лише реальні; `test`: тестові; `_all_`: усі |
| `opt_fields` | додаткові поля в елементі фіду |

`opt_fields` для `/tenders`: `dateCreated, dateModified, tenderPeriod, qualificationPeriod, auctionPeriod, awardPeriod, status, tenderID, lots, contracts, agreements, procuringEntity, procurementMethodType, procurementMethod, mode, stage2TenderID, public_modified`.
Для `/contracts`: `dateCreated, contractID, dateModified, status`.
Для `/plans`: `dateCreated, dateModified, status, planID, procuringEntity, procurementMethodType, mode`.

> Важливо: **у фіді немає `items` / CPV**. Щоб відфільтрувати тендери за товаром, доведеться завантажувати повний об'єкт `GET /tenders/{id}`. Проте вже на рівні фіду можна відсікти зайве за `status`, `procurementMethodType`, `procuringEntity` (наприклад, брати лише `complete` або `active.awarded`).

**Детальні об'єкти:**

- `GET /tenders/{id}` повертає весь тендер:
  - `items[]`: `description`, `classification` (CPV), `additionalClassifications` (наприклад, МНН для ліків), `quantity`, `unit` (код і назва), `deliveryAddress`, `relatedLot`, а також `category`, `profile`, `product` (посилання на каталог Prozorro Market);
  - `lots[]`, `value` (очікувана вартість), `procuringEntity` (замовник з ЄДРПОУ), `procurementMethodType`, `status`;
  - `awards[]`: `status` (`active` означає переможця), `suppliers[]` (`identifier.id` = ЄДРПОУ, `name`), `value`, `bid_id`, `lotID`;
  - `bids[]`: пропозиції учасників із `value`, `items[].unit.value` (**ціна за одиницю**) та `tenderers`. **Пропозиції стають публічними лише після етапу подання** (у `active.tendering` вони приховані; див. `tender/core/procedure/serializers/bid.py`);
  - `contracts[]`: короткі дані про контракти; повний контракт доступний через `/contracts/{id}`;
  - `documents[]`: `documentType`, `title`, `format`, `url`, `confidentiality`.
- `GET /contracts/{id}`: підписаний договір, `items` з `unit.value` (фінальна ціна за одиницю), `suppliers`, `value`, зміни до договору (`changes`), що корисно для виявлення подорожчання після перемоги.
- `GET /plans/{id}`: річні плани закупівель. Дають змогу **побачити майбутні закупівлі ще до оголошення тендера**.

**Документи (ТЗ):** у `tender.documents[]` поле `documentType` приймає значення на кшталт `technicalSpecifications`, `biddingDocuments` (тендерна документація), `notice`, `evaluationCriteria`, `contractProforma`, `billOfQuantity`, `winningBid`, `contractSigned` тощо (`tender/core/procedure/models/document.py`). На практиці замовники часто вкладають ТЗ як `biddingDocuments` або взагалі без типу, тому орієнтуватися варто також на назву файлу. Файли (PDF, DOCX, XLSX, часто з підписом `.p7s`) завантажуються за `url` через document service. Документи учасників можуть бути конфіденційними (`confidentiality: buyerOnly`), такі недоступні.

### 3.2. Prozorro Market / Catalog API

- Хост: в коді зустрічаються `catalog-api.prozorro.ua` і `market-api.prozorro.gov.ua`; прод-хост ще треба уточнити запитом. Swagger: `/api/doc`.
- Публічні GET-ендпоінти: `/api/categories`, `/api/profiles`, `/api/products`, `/api/products/{id}`, `/api/vendors`, `/api/offers`, `/api/tags`, `/api/search` (POST, пошук за списком id), `/api/prices`, **`/api/products/{id}/prices`**.
- **Ціни** (`src/catalog/prices.py`): каталог сам краулить тендери, бере з активних пропозицій `items[].unit.value` для товарів каталогу і за ковзним вікном у 7 днів рахує `lowerQuartile`, `medianQuartile`, `upperQuartile` та `sampleSize`.
- Корисно, щоб: знайти свої товари в каталозі (категорія або профіль з характеристиками), отримати медіанні ринкові ціни, побачити, які постачальники (vendors) пропонують ті самі товари.

### 3.3. Risks API

- Ендпоінти: `/api/risks/{tender_id}`, `/api/risks` (список із фільтрами), `/api/risks-feed`, `/api/filter-values`, `/api/risks-report`.
- Близько 30 правил (`src/prozorro/risks/rules`), що спрацьовують на тендер або контракт: ознаки неконкурентних закупівель, порушення.
- Корисно, щоб показувати «червоні прапорці» в закупівлях конкурентів. Публічний хост треба уточнити.

### 3.4. Audit API (ДАСУ)

- `/monitorings`, `/monitorings/{id}`, `/tenders/{tender_id}/monitorings`, `/inspections`, `/requests`.
- Корисно, щоб бачити, чи перевіряла ДАСУ закупівлю. Це другорядна функція.

### 3.5. Бібліотека standards

- `pip install git+https://github.com/ProzorroUKR/standards.git`
- `classifiers/dk021_uk.json` (CPV українською), `cpv_en.json`, `ua_regions.json`, `katottg.json`, `gmdn.json` (медичні вироби), кодлисти статусів і типів процедур.
- Корисно для: розшифровки CPV, підбору CPV за назвою товару, людських назв статусів і процедур.

### 3.6. Неофіційний пошук сайту prozorro.gov.ua (перевірити)

Сайт prozorro.gov.ua має власний пошуковий бекенд (фільтри за текстом, CPV, ЄДРПОУ, статусом, сумою). Він **не документований і не входить до репозиторіїв ProzorroUKR**, тому може змінитися без попередження. Його можна розглянути для швидкого пошуку кандидатів без повної синхронізації, але основою системи він бути не повинен. Потребує перевірки.

---

## 4. Як закрити наші задачі

| Задача | Звідки дані | Як |
|---|---|---|
| Знайти закупівлі з моїми товарами | `tenders` (повні об'єкти) | Локальний індекс за `items[].classification.id` (CPV, із префіксним пошуком), повнотекстовий пошук по `items[].description` і `title`, а також `items[].product` / `category` для каталожних закупівель |
| Хто переміг | `awards[status=active].suppliers[].identifier.id` | Агрегація за ЄДРПОУ: кількість перемог, сума, замовники, регіони |
| Ціна перемоги | `awards[].value`, `bids[].items[].unit.value`, `contracts/{id}.items[].unit.value` | Ціна за одиницю, знижка від очікуваної вартості, порівняння з іншими учасниками |
| Ринкова ціна товару | Catalog `/api/products/{id}/prices` + власна статистика | Медіана й квартилі за період, регіон, замовника |
| Аналіз ТЗ | `documents[]` → завантаження → текст | Витяг тексту з PDF, DOCX, XLSX (за потреби з `.p7s`), передача в Claude для порівняння з характеристиками наших товарів |
| Майбутні закупівлі | `plans` | Пошук у планах за CPV і описом |
| Подорожчання після перемоги | `contracts/{id}.changes` | Сповіщення про зміни ціни в договорі |

---

## 5. Пропонована архітектура

```
                  ┌──────────────────────────────┐
  Prozorro API ──►│ sync worker (prozorro_crawler│
  (feed + by id)  │ або власний на httpx)        │
                  └──────────────┬───────────────┘
                                 ▼
                  ┌──────────────────────────────┐
                  │ Локальна БД                  │
                  │ tenders / items / awards /   │
                  │ bids / contracts / documents │
                  │ + повнотекстовий індекс      │
                  └──────────────┬───────────────┘
                                 ▼
  Claude ◄──MCP──► MCP server (tools) ──► Catalog API, Risks API,
                                          завантаження документів «на льоту»
```

**Стек (пропозиція):** Python 3.12+, офіційний MCP Python SDK (FastMCP), httpx/aiohttp, `prozorro_crawler`, `standards`. БД: для MVP SQLite + FTS5 (один файл, нуль адміністрування); якщо обсяг виросте, перехід на PostgreSQL (JSONB + full-text).

**Обсяг даних.** У Prozorro мільйони тендерів, тож повна історія означає мільйони запитів `GET /tenders/{id}` з урахуванням rate limit. Тому:

1. фільтруємо на рівні фіду (`opt_fields=status,procurementMethodType,...`, лише завершені процедури, `mode` без тестових);
2. зберігаємо в індексі лише тендери, де CPV збігається з «моїми» кодами (список префіксів CPV у конфігурації), а решту відкидаємо;
3. backfill обмежуємо вікном (наприклад, 12–24 місяці) і далі синхронізуємось вперед у реальному часі;
4. документи не завантажуємо масово, а лише на запит (інструмент `read_document`) з кешуванням.

**Кандидати в MCP-інструменти:**

| Інструмент | Призначення |
|---|---|
| `search_tenders(query, cpv, date_from, date_to, status, region, min/max value)` | пошук закупівель у локальному індексі |
| `get_tender(id \| UA-…)` | повна картка тендера (позиції, учасники, переможець, документи) |
| `find_winners(query \| cpv, period)` | хто перемагає з подібними товарами: рейтинг постачальників |
| `get_supplier(edrpou)` | профіль конкурента: перемоги, замовники, середня знижка, товари |
| `get_winning_prices(query \| cpv, unit, period)` | статистика цін за одиницю (min, медіана, max) та список угод |
| `list_documents(tender_id)` / `read_document(tender_id, doc_id)` | ТЗ та інші документи, витягнуті в текст |
| `catalog_search(query)` / `catalog_prices(product_id)` | товари й ринкові ціни Prozorro Market |
| `search_plans(query \| cpv)` | майбутні закупівлі з річних планів |
| `get_tender_risks(tender_id)` | ризик-індикатори |
| `my_products` (ресурс або конфіг) | наші товари: назви, CPV, ключові слова, наші ціни для порівняння |

---

## 6. Етапи

1. **MVP (без БД).** MCP-сервер з інструментами `get_tender`, `list_documents`, `read_document`, `catalog_*`, що звертаються до API напряму. Це дає змогу вручну аналізувати конкретні тендери (за посиланням або ID) вже з першого дня.
2. **Синхронізатор та індекс.** Фід tenders і contracts → SQLite, фільтр за нашими CPV, backfill за вибраний період.
3. **Аналітика.** Інструменти `search_tenders`, `find_winners`, `get_supplier`, `get_winning_prices`.
4. **ТЗ.** Парсинг PDF, DOCX та XLSX (зокрема підписаних `.p7s`), кеш текстів, порівняння ТЗ з характеристиками наших товарів.
5. **Додатково.** Плани, ризики, моніторинги ДАСУ, сповіщення про нові тендери за нашими CPV.

---

## 7. Відкриті питання

1. Які товари ми продаємо? Потрібні CPV-коди (або хоча б назви, щоб підібрати CPV) і ключові слова.
2. Яка глибина історії потрібна для аналізу цін: 6, 12, 24 місяці?
3. Де працюватиме сервер: локально біля Claude Desktop або Claude Code (stdio) чи віддалено (HTTP, наприклад VPS або Cloudflare)? Від цього залежить вибір БД і синхронізатора.
4. Чи є серед наших товарів позиції з каталогу Prozorro Market? Якщо так, ціни з каталогу будуть доступні одразу.
