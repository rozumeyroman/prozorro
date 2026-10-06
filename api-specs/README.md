# OpenAPI / Swagger специфікації API Prozorro

Специфікації взято з репозиторіїв https://github.com/ProzorroUKR. Готових `swagger.json` у репозиторіях немає: сервіси генерують специфікацію з коду під час роботи (ендпоінт `/api/doc` або `/doc`). Тому файли нижче **згенеровані офлайн** з вихідного коду тим самим генератором, що працює на серверах (`aiohttp_pydantic`). Версія кожного файлу (`info.version`) містить commit, з якого його згенеровано.

| Файл | API | Джерело | Чи потрібне нам |
|---|---|---|---|
| `cdb-public-api.openapi.yaml` | **ЦБД, публічний API для читання**: фіди й об'єкти `/tenders`, `/contracts`, `/plans`, учасники (`awards`, `bids`), документи | **Написано вручну** за кодом `openprocurement.api@137ffcd` і документацією (офіційного OpenAPI немає). Описано лише потрібні нам ендпоінти та поля | **Так, основний.** Звідси беремо тендери, переможців, ціни й ТЗ |
| `catalog-api.openapi.json` | **Prozorro Market / Catalog API**: категорії, профілі, товари, оферти, постачальники, **ціни** | `prozorro-catalog@16baf1f`, згенеровано з `catalog.api:create_application` | **Так.** Товари каталогу та ринкові ціни (`/api/products/{id}/prices`, `/api/prices`). Для нас потрібні лише GET-ендпоінти; POST, PATCH і PUT доступні тільки адміністраторам каталогу |
| `risks-api.openapi.yaml` | **Risks API**: ризик-індикатори тендерів | `prozorro-risks@03c9920`, зібрано зі статичних фрагментів `swagger/*.yaml` і маршрутів у `api.py` | **Бажано.** Невеликий API, повністю read-only |
| `cdb-async-api.openapi.json` | **ЦБД, асинхронна частина** (`/api/2.5/...`) | `openprocurement.api@137ffcd`, згенеровано з `prozorro_cdb.api.main:get_aiohttp_sub_app` | **Ні.** Наразі тут описано лише `violation_reports` (повідомлення про порушення в контрактах). Збережено для повноти |

## Чого тут немає і чому

- **Офіційної** OpenAPI-специфікації для основного API ЦБД (`/tenders`, `/contracts`, `/plans` та інші) немає: він написаний на Pyramid, а документація ведеться в RST (https://prozorro-api-docs.readthedocs.io/uk/master/). Тому потрібну нам частину описано вручну у `cdb-public-api.openapi.yaml`. Під час розробки її варто звіряти з реальними відповідями API.
- **Audit API (ДАСУ)** має лише опис у форматі SPORE (`/spore`), Swagger відсутній.

## Відомі неточності (з боку генератора Prozorro)

Валідатор `openapi-spec-validator` знаходить такі проблеми; вони є і в онлайн-версіях:

- `catalog-api`: опціональні параметри описані як `anyOf: [{type: string}, {type: "null"}]`. Це синтаксис OpenAPI 3.1, хоча файл оголошено як 3.0.0.
- `cdb-async-api`: у `/ping` відсутній блок `responses`.
- `risks-api.openapi.yaml` і `cdb-public-api.openapi.yaml`: валідацію проходять без помилок.

Приклади значень у схемах (UUID, дати) генеруються випадково, тому під час перегенерації вони змінюються.

## Як оновити

Скрипти лежать у `scripts/`. Їх треба запускати в клонованих репозиторіях Prozorro зі встановленими залежностями (`uv sync --frozen --no-dev`, а також `tzdata`, якщо в системі немає бази часових поясів):

```bash
# Catalog
cd prozorro-catalog && PYTHONPATH=src .venv/bin/python ../api-specs/scripts/gen_catalog.py out.json "ProzorroUKR/prozorro-catalog@$(git rev-parse --short HEAD)"
# CDB (async)
cd openprocurement.api && PYTHONPATH=src .venv/bin/python ../api-specs/scripts/gen_cdb.py out.json "ProzorroUKR/openprocurement.api@$(git rev-parse --short HEAD)"
# Risks
python gen_risks.py prozorro-risks/swagger out.yaml "ProzorroUKR/prozorro-risks@$(git -C prozorro-risks rev-parse --short HEAD)"
```

Коли буде доступ до мережі Prozorro, специфікації можна буде звірити з онлайн-версіями: `<host>/api/doc` для Catalog і Risks, `<host>/api/2.5/doc` для ЦБД.
