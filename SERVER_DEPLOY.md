# Развёртывание arvento_report на чистом сервере

Production-схема рассчитана на Ubuntu, Docker Compose и systemd. Рекомендуемый путь проекта — `/opt/arvento_report`. PostgreSQL хранится в Docker volume, резервные копии — вне Git checkout.

## 1. Предварительные требования

```bash
git --version
docker --version
docker compose version
python3 --version
curl --version
flock --version
timeout --version
systemctl is-active docker
```

Рекомендуемый минимум: 4 vCPU, 8 ГиБ RAM, настроенный swap и отдельный запас диска под PostgreSQL и backup. Порты PostgreSQL, 18083 и 18084 не должны быть доступны из внешней сети.

## 2. Клонирование и `.env`

```bash
cd /opt
git clone https://github.com/m9117882424/arvento-kpp-report.git arvento_report
cd /opt/arvento_report
cp .env.server.example .env
chmod 600 .env
nano .env
```

Обязательные параметры:

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

Для чистой установки используйте URL-safe пароль:

```bash
openssl rand -hex 32
```

Одинаковое значение указывается в `POSTGRES_PASSWORD` и внутри `DATABASE_URL`:

```text
DATABASE_URL=postgresql://arvento_report:<пароль>@postgres:5432/arvento_report
```

`.env` не отслеживается Git и исключён из Docker build context.

## 3. Установка

```bash
sudo bash deploy/install.sh /opt/arvento_report
```

Installer:

1. проверяет зависимости и обязательные переменные;
2. запускает `verify_repository.py` и `verify_deployment.py`;
3. проверяет `docker compose config`;
4. собирает image вместе со smoke-тестами;
5. запускает `postgres`, `geofence-editor`, `report-portal`;
6. устанавливает scripts и systemd units;
7. включает синхронизацию, ночную коррекцию и backup;
8. проверяет HTTP health endpoints.

Установка без включения timers:

```bash
sudo INSTALL_ENABLE_TIMERS=0 bash deploy/install.sh /opt/arvento_report
```

## 4. Production stack

Обычный запуск:

```bash
docker compose -f docker-compose.server.yml up -d
```

запускает только:

```text
postgres
geofence-editor
report-portal
```

`gps-sync` находится в профиле `legacy-daemon` и не входит в default stack. Это исключает одновременную работу бесконечного Docker-демона и systemd pipeline.

## 5. Расписание

```text
arvento-intraday-pipeline.timer   каждые 30 минут, 05:00–23:30 Europe/Istanbul
arvento-nightly-correction.timer  ежедневно в 00:10 Europe/Istanbul
arvento-backup.timer              ежедневно в 03:30 Europe/Istanbul
```

Проверка:

```bash
systemctl list-timers --all --no-pager | grep -E 'arvento-(intraday|nightly|backup)'
```

Внутридневный pipeline загружает последние шесть часов и пересчитывает текущий день только после статуса `SUCCESS`. Ночная коррекция загружает предыдущие сутки. Backup создаётся в custom-format и проверяется командой `pg_restore --list`.

## 6. Защита от зависаний

Общие блокировки:

```text
/run/arvento-sync-and-cache.lock
/run/arvento-consolidated-cache.lock
```

Timeout:

```text
PIPELINE_INTRADAY_TIMEOUT_SECONDS=2700
PIPELINE_NIGHTLY_TIMEOUT_SECONDS=7200
PIPELINE_CACHE_TIMEOUT_SECONDS=3600
```

Контейнеры имеют ограничения CPU, RAM, PID, tmpfs и ротацию JSON-логов. Не запускайте полносуточную тестовую синхронизацию параллельно production pipeline. Для диагностики применяйте короткий read-only диапазон либо отдельную тестовую базу.

## 7. Проверка состояния

```bash
sudo /usr/local/sbin/arvento-healthcheck
```

Дополнительно:

```bash
docker compose -f docker-compose.server.yml ps
curl http://127.0.0.1:18083/health
curl http://127.0.0.1:18084/health

journalctl -u arvento-intraday-pipeline.service -n 200 --no-pager
journalctl -u arvento-nightly-correction.service -n 200 --no-pager
journalctl -u arvento-backup.service -n 100 --no-pager
```

## 8. Ручные операции

Полная штатная цепочка:

```bash
sudo /usr/local/sbin/arvento-sync-and-cache intraday
sudo /usr/local/sbin/arvento-sync-and-cache nightly
```

Только загрузка последних шести часов:

```bash
cd /opt/arvento_report
flock -n /run/arvento-sync-and-cache.lock \
  docker compose -f docker-compose.server.yml run --rm --no-deps report-portal \
  python sync_arvento_gps_to_postgres.py recent --hours 6
```

Очистка старых GPS-партиций:

```bash
flock -n /run/arvento-sync-and-cache.lock \
  docker compose -f docker-compose.server.yml run --rm --no-deps report-portal \
  python sync_arvento_gps_to_postgres.py retention
```

Конкретные сутки запускайте только при реальной необходимости и после проверки нагрузки:

```bash
flock -n /run/arvento-sync-and-cache.lock \
  docker compose -f docker-compose.server.yml run --rm --no-deps report-portal \
  python sync_arvento_gps_to_postgres.py day 2026-07-24
```

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

Перед восстановлением остановите timers, создайте дополнительный backup и проверьте имя базы/пользователя в `.env`. После восстановления включите timers обратно.

## 10. Nginx

Сервисы слушают только localhost:

```text
127.0.0.1:18083
127.0.0.1:18084
```

Пример reverse proxy с TLS и Basic Auth:

```text
deploy/nginx/arvento-report.conf.example
```

Перед включением замените домен и certificate paths, создайте htpasswd, выполните `nginx -t` и только затем перезагрузите Nginx.

## 11. Обновление и rollback

Перед обновлением:

```bash
sudo systemctl start arvento-backup.service
```

Обновление:

```bash
cd /opt/arvento_report
git fetch origin
git status --short --branch
git pull --ff-only origin main
sudo bash deploy/install.sh /opt/arvento_report
```

Rollback к известному commit:

```bash
cd /opt/arvento_report
git fetch origin
git checkout --detach <COMMIT_SHA>
sudo bash deploy/install.sh /opt/arvento_report
```

Installer не удаляет PostgreSQL volume.

## 12. Репозиторные проверки

```bash
python3 verify_repository.py
python3 verify_deployment.py
docker compose -f docker-compose.server.yml config --quiet
docker build -f Dockerfile.server -t arvento-report:test .
```

Проверяются deployment manifests, конечные timeout, общий lock, ссылки Dockerfile, отсутствие секретов и runtime-артефактов, resource limits, геозоны, KML и сборка server image.

## 13. Канонические команды и compatibility-модули

Production entrypoints:

```text
sync_arvento_gps_to_postgres.py
run_geofence_editor.py
run_report_portal.py
generate_kpp_summary_report.py
generate_first_entry_report.py
generate_prohibited_left_turn_report.py
generate_consolidated_report.py
generate_scheduled_reports.py
```

Старые реализации `arvento_postgres_sync_v2.py`, `arvento_first_entry_report_fixed.py` и другие compatibility-модули пока удалять нельзя: канонические wrappers всё ещё импортируют их. Они не должны использоваться в Docker Compose, systemd или новых инструкциях. Удаление выполняется отдельным рефакторингом с тестами.
