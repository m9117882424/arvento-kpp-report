# arvento_report

Система загрузки телематических данных Arvento, хранения GPS-точек в PostgreSQL/PostGIS, управления геозонами и формирования транспортных отчётов.

## Основные задачи

1. Получение `GeneralReportWithDistance` из Arvento API.
2. Сохранение GPS-точек, скорости, расстояния, простоев и справочной геозоны Arvento.
3. Хранение собственных геозон и их версий в PostGIS.
4. Формирование отчётов по КПП, первому въезду, эффективности транспорта и запрещённому повороту.
5. Расчёт пробега, времени внутри площадки, нарушений и показателей эффективности.

## Производственная схема

По умолчанию Docker Compose запускает только постоянные сервисы:

- `postgres` — PostgreSQL 16 + PostGIS;
- `geofence-editor` — редактор геозон на localhost;
- `report-portal` — портал отчётов на localhost.

Синхронизация не запускается вторым бесконечным Docker-демоном. Её выполняют systemd timers:

- `arvento-intraday-pipeline.timer` — каждые 30 минут с 01:00 до 23:30;
- `arvento-nightly-correction.timer` — корректировка предыдущих суток в 00:10;
- `arvento-backup.timer` — проверенный `pg_dump` в 03:30.

Все тяжёлые задания используют общий `flock`, конечные timeout и ограничения ресурсов контейнера. Сервис `gps-sync` сохранён только в профиле `legacy-daemon` для совместимости и не входит в обычный `docker compose up`.

## Развёртывание на чистом Ubuntu-сервере

Требуются Git, Docker Engine, Docker Compose plugin, `curl`, `flock`, `timeout` и Python 3.

```bash
cd /opt
git clone https://github.com/m9117882424/arvento-kpp-report.git arvento_report
cd /opt/arvento_report
cp .env.server.example .env
nano .env
sudo bash deploy/install.sh /opt/arvento_report
```

Для `POSTGRES_PASSWORD` используйте URL-safe значение, поскольку этот пароль входит в `DATABASE_URL`:

```bash
openssl rand -hex 32
```

Одинаковое значение необходимо указать в `POSTGRES_PASSWORD` и внутри `DATABASE_URL`.

Installer:

- проверяет обязательные переменные;
- запускает `verify_repository.py` и `verify_deployment.py`;
- валидирует Docker Compose;
- собирает image вместе со smoke-тестами;
- запускает только core stack;
- устанавливает production scripts и systemd units;
- включает синхронизацию, ночную коррекцию и резервное копирование;
- проверяет health endpoints.

Полная инструкция: [`SERVER_DEPLOY.md`](SERVER_DEPLOY.md).

## Проверка после установки

```bash
sudo /usr/local/sbin/arvento-healthcheck

docker compose -f docker-compose.server.yml ps
systemctl list-timers --all --no-pager | grep -E 'arvento-(intraday|nightly|backup)'

curl http://127.0.0.1:18083/health
curl http://127.0.0.1:18084/health
```

Логи:

```bash
journalctl -u arvento-intraday-pipeline.service -n 200 --no-pager
journalctl -u arvento-nightly-correction.service -n 200 --no-pager
journalctl -u arvento-backup.service -n 100 --no-pager
```

## Ручная синхронизация

Перед ручным запуском остановите соответствующий timer либо используйте тот же общий lock. Production wrapper:

```bash
sudo /usr/local/sbin/arvento-sync-and-cache intraday
sudo /usr/local/sbin/arvento-sync-and-cache nightly
```

Только загрузка последних шести часов без расчёта кэша:

```bash
flock -n /run/arvento-sync-and-cache.lock \
  docker compose -f docker-compose.server.yml run --rm --no-deps report-portal \
  python sync_arvento_gps_to_postgres.py recent --hours 6
```

Конкретные сутки загружаются только при реальной необходимости:

```bash
flock -n /run/arvento-sync-and-cache.lock \
  docker compose -f docker-compose.server.yml run --rm --no-deps report-portal \
  python sync_arvento_gps_to_postgres.py day 2026-07-24
```

## Обновление

```bash
cd /opt/arvento_report
git fetch origin
git pull --ff-only origin main
sudo bash deploy/install.sh /opt/arvento_report
```

`install.sh` повторяемый: он пересобирает проверенный image, обновляет units и сохраняет существующий PostgreSQL volume.

## Резервные копии

По умолчанию backup хранится вне Git checkout в `/opt/arvento_backups` и удаляется через 14 дней. Параметры:

```text
BACKUP_DIR=/opt/arvento_backups
BACKUP_RETENTION_DAYS=14
```

Ручной запуск:

```bash
sudo systemctl start arvento-backup.service
journalctl -u arvento-backup.service -n 100 --no-pager
```

Backup принимается только после проверки `pg_restore --list`.

## Портал и Nginx

Порты привязаны только к localhost:

```text
geofence-editor: 127.0.0.1:18083
report-portal:   127.0.0.1:18084
```

Пример reverse proxy с HTTPS и Basic Auth находится в `deploy/nginx/arvento-report.conf.example`. Порты 18083/18084 не требуется открывать в UFW.

Защищённый JSON API для внешней панели легкового автопарка объединяет готовые показатели Arvento с топливом Fuel Monitor без формирования Excel. Контракт, переменные окружения и настройка отдельного Nginx path описаны в [`docs/fleet-dashboard-api.md`](docs/fleet-dashboard-api.md).

## Канонические исполняемые файлы

- `sync_arvento_gps_to_postgres.py` — загрузка GPS в PostgreSQL/PostGIS;
- `run_geofence_editor.py` — веб-редактор геозон;
- `run_report_portal.py` — базовый портал отчётов;
- `generate_kpp_summary_report.py` — сводный отчёт по КПП;
- `generate_first_entry_report.py` — отчёт по первому въезду;
- `generate_prohibited_left_turn_report.py` — отчёт о запрещённом повороте;
- `generate_consolidated_report.py` — сводный отчёт;
- `generate_scheduled_reports.py` — пакетный запуск отчётов.

Файлы `arvento_postgres_sync_v2.py`, `arvento_first_entry_report_fixed.py` и другие старые имена пока остаются внутренними compatibility-модулями: канонические wrappers их импортируют. Они не являются отдельными production entrypoints и не должны использоваться в Compose, systemd или новых инструкциях.

## Репозиторные проверки

```bash
python3 verify_repository.py
python3 verify_deployment.py
docker compose -f docker-compose.server.yml config --quiet
docker build -f Dockerfile.server -t arvento-report:test .
```

Проверяются:

- Python-синтаксис и runtime smoke-тесты;
- наличие канонических entrypoints;
- ссылки Dockerfile только на существующие файлы;
- systemd timers, locks и конечные timeout;
- отсутствие отслеживаемых `.env`, ключей, backup, Excel/CSV/SQLite и других runtime-артефактов;
- геозоны и KML;
- успешная сборка server image.

GitHub Actions выполняет эти проверки для pull request и `main`.

## Правила расчёта и источники

- Канонические пороги расчёта находятся в `business_rules.py` и одинаковы для портала и CLI.
- Пробег `VehicleDistanceReport` остаётся итоговым; координатный пробег используется для контроля на листе `Проверка пробега`.
- Разнарядка применяется на точную дату либо берётся последняя предыдущая. Будущая разнарядка никогда не применяется задним числом.
- При `GEOFENCE_SOURCE=auto` отчёты используют полный активный набор версий PostGIS. Если набор неполный, они явно журналируют причину и используют статические геозоны.
- Миграции PostgreSQL выполняются при запуске портала под advisory lock и регистрируются с контрольной суммой.
