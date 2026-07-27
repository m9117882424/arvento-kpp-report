# arvento_report

Система загрузки телематических данных Arvento, хранения GPS-точек в PostgreSQL/PostGIS, управления собственными геозонами и формирования транспортных отчётов.

## Основные задачи

1. Получение `GeneralReportWithDistance` из Arvento API.
2. Сохранение GPS-точек, скорости, расстояния, простоев, зажигания и справочной геозоны Arvento.
3. Хранение собственных геозон и их версий в PostGIS.
4. Формирование отчётов по КПП, первому въезду, эффективности легкового транспорта и запрещённому повороту.
5. Подготовка расчётов пробега, простоев, нарушений и эффективности.

## Канонические имена исполняемых файлов

- `sync_arvento_gps_to_postgres.py` — загрузка GPS из Arvento API в PostgreSQL/PostGIS;
- `run_geofence_editor.py` — веб-редактор собственных геозон;
- `generate_kpp_summary_report.py` — сводный отчёт по КПП и эффективности легкового транспорта;
- `generate_first_entry_report.py` — отчёт по первому въезду;
- `generate_prohibited_left_turn_report.py` — отчёт о запрещённом повороте налево;
- `generate_scheduled_reports.py` — пакетный запуск отчётов по расписанию.

Внутренние модули:

- `arvento_api_client.py` — клиент Arvento API;
- `parse_arvento_general_report.py` — разбор XML `GeneralReportWithDistance`.

Старые имена временно оставлены как совместимые внутренние реализации, чтобы не ломать ранее созданные команды. Новые команды, Docker Compose, портал и документация используют только канонические имена.

## Docker-сервисы

- `postgres` — PostgreSQL 16 + PostGIS;
- `gps-sync` — регулярная синхронизация Arvento;
- `geofence-editor` — веб-редактор геозон;
- `report-portal` — временная веб-страница формирования отчётов.

Имя Compose-проекта: `arvento_report`.

## Установка на сервер

```bash
git clone https://github.com/m9117882424/arvento-kpp-report.git arvento_report
cd arvento_report
cp .env.server.example .env
nano .env
docker compose -f docker-compose.server.yml up -d --build
```

Проверка:

```bash
docker compose -f docker-compose.server.yml ps
docker compose -f docker-compose.server.yml logs -f gps-sync
curl http://127.0.0.1:18083/health
curl http://127.0.0.1:18084/health
python verify_repository.py
```

## Ручная синхронизация

Последние 6 часов:

```bash
docker compose -f docker-compose.server.yml run --rm gps-sync \
  python sync_arvento_gps_to_postgres.py recent --hours 6
```

Конкретные сутки:

```bash
docker compose -f docker-compose.server.yml run --rm gps-sync \
  python sync_arvento_gps_to_postgres.py day 2026-07-24
```

Очистка GPS старше срока хранения:

```bash
docker compose -f docker-compose.server.yml run --rm gps-sync \
  python sync_arvento_gps_to_postgres.py retention
```

## Портал отчётов

Портал слушает только localhost на порту `${REPORT_PORTAL_PORT:-18084}`. Для внешнего доступа используется Nginx с HTTPS и авторизацией.

Доступные отчёты:

- первый въезд через КПП;
- эффективность легкового транспорта;
- запрещённый поворот.

GPS выгружается из PostgreSQL во временный CSV. Промежуточные CSV и Excel удаляются после формирования ответа браузеру.

## Отчёты на Windows

Сводный отчёт по КПП и эффективности:

```powershell
python generate_kpp_summary_report.py "C:\Reports\Report.csv"
```

Первый въезд:

```powershell
python generate_first_entry_report.py "Report.xlsx" "Разнарядка.xlsx" "Первый въезд.xlsx" --time-from 07:00 --time-to 09:00
```

Запрещённый поворот:

```powershell
python generate_prohibited_left_turn_report.py "C:\Reports\Report.csv"
```

Пакетный запуск:

```powershell
python generate_scheduled_reports.py
```

## Геозоны

Основным источником для расчётов являются собственные геозоны PostGIS. Геозона, возвращённая Arvento, сохраняется только как справочное поле `region_name`.

Редактор использует Google Satellite при наличии ключа. Если ключ отсутствует или карта Google не загрузилась, автоматически используется OpenStreetMap.

## Поддерживаемые исходные форматы

Для локальных отчётов поддерживаются `.xlsx`, `.xlsm` и `.csv`. Крупные файлы импортируются во временную SQLite-базу и обрабатываются по автомобилям и датам.

## Проверка согласованности репозитория

```bash
python verify_repository.py
```

Проверяются:

- наличие канонических исполняемых файлов;
- ссылки на них в Docker, портале и документации;
- отсутствие лишних неканонических entrypoint-файлов;
- синтаксис всех Python-файлов.

## Именование

Правило для новых исполняемых файлов:

```text
<действие>_<объект>_<результат>.py
```

Примеры:

```text
sync_arvento_gps_to_postgres.py
generate_kpp_summary_report.py
generate_first_entry_report.py
generate_prohibited_left_turn_report.py
```

Имена вида `fixed`, `v2`, `new`, `final` в канонических исполняемых файлах не используются. Версии алгоритмов хранятся в базе и Git, а не в имени файла.
