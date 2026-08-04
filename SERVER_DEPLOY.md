# Развёртывание arvento_report на чистом сервере

Документ описывает production-схему для Ubuntu, Docker Compose и systemd. Рекомендуемый путь проекта: `/opt/arvento_report`. Данные PostgreSQL хранятся в отдельном Docker volume, backup — вне Git checkout.

## 1. Предварительные требования

Проверьте наличие:

```bash
git --version
docker --version
docker compose version
python3 --version
curl --version
flock --version
timeout --version
```

Docker Engine должен быть установлен из доверенного репозитория и запущен:

```bash
systemctl enable --now docker
systemctl is-active docker
```

Рекомендуемый минимум для production:

- 4 vCPU;
- 8 ГиБ RAM;
- настроенный swap;
- отдельный запас диска под PostgreSQL и backup;
- корректное время `Europe/Istanbul`;
- закрытые внешним firewall порты PostgreSQL, 18083 и 18084.

## 2. Клонирование

```bash
cd /opt
git clone https://github.com/m9117882424/arvento-kpp-report.git arvento_report
cd /opt/arvento_report
git status --short --branch
```

Создайте конфигурацию:

```bash
cp .env.server.example .env
chmod 600 .env
nano .env
```

Обязательные значения:

```text
POSTGRES_DB
POSTGRES_USER
POSTGRES_PASSWORD
DATABASE_URL
ARVENTO_USER
ARVENTO_PIN1
ARVENTO_PIN2
ARVENTO_GROUP
```

Создайте URL-safe пароль:

```bash
openssl rand -hex 32
```

Укажите одно и то же значение:

```text
POSTGRES_PASSWORD=<пароль>
DATABASE_URL=postgresql://arvento_report:<пароль>@postgres:5432/arvento_report
```

Пароли с `@`, `:`, `/`, `?`, `#`, `[` и `]` требуют URL-кодирования. Installer запрещает такие значения для чистой установки, чтобы исключить скрытую ошибку подключения.

Файл `.env` не добавляется в Git и не попадает в Docker build context.

## 3. Автоматическая установка

```bash
cd /opt/arvento_report
sudo bash deploy/install.sh /opt/arvento_report
```

Installer выполняет:

1. проверку зависимостей и `.env`;
2. `verify_repository.py`;
3. `verify_deployment.py`;
4. `docker compose config --quiet`;
5. сборку image со smoke-тестами;
6. запуск `postgres`, `geofence-editor`, `report-portal`;
7. установку scripts в `/usr/local/sbin`;
8. установку systemd units/timers;
9. включение синхронизации и backup;
10. HTTP health checks.

Установить units, но не включать timers:

```bash
sudo INSTALL_ENABLE_TIMERS=0 bash deploy/install.sh /opt/arvento_report
```

## 4. Состав production stack

Обычный запуск:

```bash
docker compose -f docker-compose.server.yml up -d
```

поднимает только:

```text
postgres
geofence-editor
report-portal
```

`gps-sync` находится в профиле `legacy-daemon` и по умолчанию не запускается. Это исключает одновременную работу бесконечного Docker-демона и systemd pipeline.

Для просмотра итоговой конфигурации:

```bash
docker compose -f docker-compose.server.yml config
```

## 5. Расписание

### Внутридневная синхронизация

```text
arvento-intraday-pipeline.timer
05:00–23:30 Europe/Istanbul, каждые 30 минут
```

Pipeline:

1. получает последние шесть часов Arvento;
2. проверяет статус новой записи `sync_runs`;
3. только при `SUCCESS` пересчитывает кэш текущего дня.

### Ночная корректировка

```text
arvento-nightly-correction.timer
00:10 Europe/Istanbul
```

Загружает предыдущие сутки и пересчитывает их кэш.

### Резервное копирование

```text
arvento-backup.timer
03:30 Europe/Istanbul
```

Создаёт custom-format `pg_dump`, проверяет его командой `pg_restore --list`, затем применяет retention.

Проверка timers:

```bash
systemctl list-timers --all --no-pager | grep -E 'arvento-(intraday|nightly|backup)'
```

## 6. Защита от зависаний и повторных запусков

Pipeline использует:

```text
/run/arvento-sync-and-cache.lock
/run/arvento-consolidated-cache.lock
```

Внутридневный и ночной запуск не могут выполняться одновременно. Для каждого этапа задан конечный timeout:

```text
PIPELINE_INTRADAY_TIMEOUT_SECONDS=2700
PIPELINE_NIGHTLY_TIMEOUT_SECONDS=7200
PIPELINE_CACHE_TIMEOUT_SECONDS=3600
```

Docker-контейнеры имеют ограничения CPU, RAM, PID, размер tmpfs и ротацию JSON-логов. Значения задаются в `.env`.

Не запускайте синхронизацию полного дня напрямую параллельно systemd pipeline. Для диагностических тестов используйте короткий read-only диапазон либо отдельную тестовую базу.

## 7. Проверка состояния

Комплексная read-only проверка:

```bash
sudo /usr/local/sbin/arvento-healthcheck
```

Она проверяет:

- состояние Compose-контейнеров;
- health endpoints;
- timers;
- последние `sync_runs`;
- записи `RUNNING` для ручной проверки;
- RAM и диск.

Отдельные команды:

```bash
docker compose -f docker-compose.server.yml ps
curl http://127.0.0.1:18083/health
curl http://127.0.0.1:18084/health
```

Логи:

```bash
journalctl -u arvento-intraday-pipeline.service -n 200 --no-pager
journalctl -u arvento-nightly-correction.service -n 200 --no-pager
journalctl -u arvento-backup.service -n 100 --no-pager

docker compose -f docker-compose.server.yml logs --tail=200 postgres
docker compose -f docker-compose.server.yml logs --tail=200 report-portal
docker compose -f docker-compose.server.yml logs --tail=200 geofence-editor
```

## 8. Ручные операции

Полная штатная цепочка:

```bash
sudo /usr/local/sbin/arvento-sync-and-cache intraday
sudo /usr/local/sbin/arvento-sync-and-cache nightly
```

Только синхронизация последних шести часов:

```bash
cd /opt/arvento_report
flock -n /run/arvento-sync-and-cache.lock \
  docker compose -f docker-compose.server.yml run --rm --no-deps report-portal \
  python sync_arvento_gps_to_postgres.py recent --hours 6
```

Очистка старых GPS-партиций:

```bash
cd /opt/arvento_report
flock -n /run/arvento-sync-and-cache.lock \
  docker compose -f docker-compose.server.yml run --rm --no-deps report-portal \
  python sync_arvento_gps_to_postgres.py retention
```

Конкретные сутки:

```bash
cd /opt/arvento_report
flock -n /run/arvento-sync-and-cache.lock \
  docker compose -f docker-compose.server.yml run --rm --no-deps report-portal \
  python sync_arvento_gps_to_postgres.py day 2026-07-24
```

Такой запуск может быть тяжёлым. Перед ним проверьте нагрузку, свободную память, отсутствие активного pipeline и необходимость операции.

## 9. Backup и восстановление

Настройки:

```text
BACKUP_DIR=/opt/arvento_backups
BACKUP_RETENTION_DAYS=14
```

Ручной backup:

```bash
systemctl start arvento-backup.service
journalctl -u arvento-backup.service -n 100 --no-pager
ls -lh /opt/arvento_backups
```

Восстановление выполняйте в запланированное окно:

```bash
cd /opt/arvento_report
systemctl stop arvento-intraday-pipeline.timer arvento-nightly-correction.timer

docker compose -f docker-compose.server.yml exec -T postgres \
  dropdb -U arvento_report --if-exists arvento_report

docker compose -f docker-compose.server.yml exec -T postgres \
  createdb -U arvento_report arvento_report

docker compose -f docker-compose.server.yml exec -T postgres \
  pg_restore -U arvento_report -d arvento_report --clean --if-exists \
  < /opt/arvento_backups/arvento_report_YYYYMMDD_HHMMSS.dump

systemctl start arvento-intraday-pipeline.timer arvento-nightly-correction.timer
```

Перед восстановлением сохраните текущий backup и проверьте имя пользователя/базы из `.env`.

## 10. Nginx и внешний доступ

Порты приложений слушают только localhost:

```text
127.0.0.1:18083
127.0.0.1:18084
```

Пример:

```text
deploy/nginx/arvento-report.conf.example
```

Перед включением:

1. замените `reports.example.com`;
2. получите TLS-сертификат;
3. создайте Basic Auth;
4. выполните `nginx -t`;
5. перезагрузите Nginx.

Порты 18083 и 18084 не открывайте во внешний интерфейс.

## 11. Обновление

Сначала создайте backup:

```bash
sudo systemctl start arvento-backup.service
```

Затем:

```bash
cd /opt/arvento_report
git fetch origin
git status --short --branch
git pull --ff-only origin main
sudo bash deploy/install.sh /opt/arvento_report
```

Installer не удаляет PostgreSQL volume.

Rollback к известному commit:

```bash
cd /opt/arvento_report
git fetch origin
git checkout --detach <COMMIT_SHA>
sudo bash deploy/install.sh /opt/arvento_report
```

После проверки создайте отдельную rollback-ветку либо вернитесь на `main`. Не используйте `git reset --hard` при наличии несохранённых локальных изменений.

## 12. Проверка репозитория до развёртывания

```bash
python3 verify_repository.py
python3 verify_deployment.py
docker compose -f docker-compose.server.yml config --quiet
docker build -f Dockerfile.server -t arvento-report:test .
```

`verify_deployment.py` проверяет:

- все deployment manifests;
- отсутствие бесконечных timeout;
- общий lock;
- ссылки Dockerfile только на существующие файлы;
- отсутствие отслеживаемых `.env`, ключей, архивов, backup, Excel/CSV/SQLite и runtime-каталогов;
- наличие resource/log limits;
- обязательные timers и scripts.

GitHub Actions выполняет те же проверки для pull request и `main`.

## 13. Legacy-модули

Старые файлы, включая `arvento_postgres_sync_v2.py` и `arvento_first_entry_report_fixed.py`, пока остаются внутренними compatibility-модулями. Канонические wrappers импортируют их, поэтому удалять их сейчас нельзя.

Они не должны использоваться как отдельные production entrypoints. Последующий рефакторинг должен переносить реализацию в канонические модули по одному компоненту с тестами, после чего legacy-файл можно удалить отдельным PR.
