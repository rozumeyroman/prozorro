# Prozorro MCP

MCP-сервер для Claude, який знаходить і аналізує публічні закупівлі Prozorro за темами **кібербезпека, мережеве та серверне обладнання** з очікуваною вартістю від **500 000 грн**.

Дані беруться з публічного API Prozorro (`public-api.prozorro.gov.ua`). Релевантні тендери зберігаються в локальній базі SQLite.

- План розробки: [`docs/development-plan.md`](docs/development-plan.md)
- Як працює фільтр: [`docs/tender-filter.md`](docs/tender-filter.md); налаштування в [`config/tender-filter.yaml`](config/tender-filter.yaml)
- Дослідження API: [`docs/research/prozorro-api.md`](docs/research/prozorro-api.md); специфікації в [`api-specs/`](api-specs/)

## Інструменти MCP

| Інструмент | Що робить |
|---|---|
| `sync_tenders` | Підтягує з Prozorro тендери за період (`today`, `yesterday`, `24h`, `3d`, `2026-10-01`) і зберігає ті, що пройшли фільтр. Повертає статистику відсіювання |
| `search_tenders` | Пошук у локальній базі: текст, тема (`network`, `servers_storage`, `cybersecurity`, `keyword`), мінімальна вартість, дати, статус, сортування |
| `get_tender` | Картка тендера за id, номером `UA-…` або посиланням: позиції, лоти, учасники та їхні ціни за одиницю, переможці, договори, документи |
| `list_documents` | Документи тендера з посиланнями на файли |
| `export_excel` | Вивантажує тендери з бази в Excel: аркуші «Тендери», «Позиції», «Переможці», «Пропозиції», «Ціни за одиницю». Фільтри: стадія (тривають / завершені), тема, текст, дати створення та визначення переможця |
| `download_documents` | Завантажує тендерну документацію (за потреби й документи пропозицій учасників) у теки «Замовник - Предмет - UA-ID» |
| `explain_filter` | Чому тендер пройшов чи не пройшов фільтр (для налаштування конфігу) |
| `status` | Стан бази, параметри фільтра, остання синхронізація |

## Встановлення

Потрібні Python 3.11+ і [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
git clone https://github.com/rozumeyroman/prozorro.git
cd prozorro
uv sync
```

Перевірка без Claude:

```bash
uv run prozorro-mcp sync --since today   # завантажити сьогоднішні тендери
uv run prozorro-mcp search комутатор     # пошук у локальній базі
uv run prozorro-mcp export --stage complete --awarded-from yesterday   # Excel з переможцями за вчора й сьогодні
uv run prozorro-mcp docs --stage active  # документація тендерів, що тривають
```

## Файли на диску

Результати зберігаються в теці `~/Prozorro` (на Windows `C:\Users\<ім'я>\Prozorro`):

- `Експорт/`: файли Excel. Кожен запит створює новий файл з датою й часом у назві, якщо не задати власну назву.
- `Документи/<Замовник> - <Предмет закупівлі> - <UA-ID>/`: документи тендера; документи учасників лежать у підтеці `Пропозиції учасників/`. Файл `_documents.json` у теці дає змогу при повторному запуску докачати лише нові або змінені документи.

Підписи `.p7s` за замовчуванням не завантажуються. Документи з позначкою «конфіденційно» (`buyerOnly`) недоступні публічно й пропускаються. Файли завантажуються лише з хостів `*.prozorro.gov.ua`.

## Підключення до Claude Desktop

Відкрийте файл конфігурації Claude Desktop (Settings → Developer → Edit Config):

- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

Додайте сервер, вказавши **абсолютний** шлях до теки проєкту:

```json
{
  "mcpServers": {
    "prozorro": {
      "command": "uv",
      "args": ["run", "--directory", "C:\\Users\\you\\prozorro", "prozorro-mcp"]
    }
  }
}
```

На macOS шлях має вигляд `/Users/you/prozorro`. Якщо Claude Desktop не знаходить `uv`, вкажіть повний шлях до нього: `where uv` на Windows або `which uv` на macOS.

Перезапустіть Claude Desktop. У списку інструментів з'явиться `prozorro`. Приклади запитів:

- «Підтягни нові тендери за сьогодні і покажи, що знайшлося»
- «Які тендери на мережеве обладнання понад 1 млн грн зараз приймають пропозиції?»
- «Хто переміг у UA-2026-10-06-000123-a і з якою ціною за одиницю?»
- «Вивантаж в Excel завершені тендери, де переможця визначили за останній тиждень»
- «Завантаж документацію тендерів на мережеве обладнання, що зараз приймають пропозиції»

## Підключення до Claude Code

У репозиторії є `.mcp.json`, тому Claude Code, запущений у теці проєкту, запропонує підключити сервер `prozorro`. Підтвердьте це під час першого запуску.

## Налаштування

| Змінна середовища | За замовчуванням | Призначення |
|---|---|---|
| `PROZORRO_API_URL` | `https://public-api.prozorro.gov.ua/api/2.5` | API ЦБД (для тестів можна вказати sandbox) |
| `PROZORRO_DB` | `data/prozorro.db` у теці проєкту | Файл бази SQLite |
| `PROZORRO_FILTER_CONFIG` | `config/tender-filter.yaml` | Правила фільтра |
| `PROZORRO_OUTPUT_DIR` | `~/Prozorro` | Тека для Excel-файлів і документів |
| `PROZORRO_CONCURRENCY` | `4` | Паралельні запити повних тендерів |

Змінні можна передати в `env` у конфігурації Claude Desktop.

Перший `sync_tenders` за великий період (кілька днів і більше) може тривати довше, ніж Claude чекає на відповідь інструмента. Такий період краще завантажити з терміналу: `uv run prozorro-mcp sync --since 7d`.

## Розробка

```bash
uv run --group dev pytest        # тести, зокрема наскрізний тест MCP через stdio
uv run --group dev ruff check src tests
```

Тести не звертаються до мережі. Вони використовують імітацію API (`tests/fake_prozorro.py`), побудовану на реальному прикладі тендера з документації Prozorro.
