# Развёртывание arvento_report на сервере

## 1. Клонирование

```bash
cd /opt
git clone https://github.com/m9117882424/arvento-kpp-report.git arvento_report
cd /opt/arvento_report
cp .env.server.example .env
nano .env
```

Обязательные параметры:

```text
POSTGRES_PASSWORD
ARVENTO_USER
ARVENTO_PIN1
ARVENTO_PIN2
ARVENTO_GROUP
```

Дополнительные параметры:

```text
GOOGLE_MAPS_API_KEY
GEOFENCE_EDITOR_PORT=18083
REPORT_PORTAL_PORT=18084
ARVENTO_RETENTION_DAYS
```

При пустом или нерабочем `GOOGLE_MAPS_API_KEY` редактор геозон использует OpenStreetMap. Файл `.env` не добавлять в Git.

## 2. Проверка репозитория и конфигурации

```bash
python verify_repository.py
docker compose -f docker-compose.server.yml config
```

Compose-проект имеет имя `arvento_report`. Сервисы:

```text
postgres
gps-sync
geofence-editor
report-portal
```

## 3. Запуск

```bash
docker compose -f docker-compose.server.yml up -d --build
```

Проверка:

```bash
docker compose -f docker-compose.server.yml ps
docker compose -f docker-compose.server.yml logs --tail=100 gps-sync
docker compose -f docker-compose.server.yml logs --tail=100 geofence-editor
docker compose -f docker-compose.server.yml logs --tail=100 report-portal
curl http://127.0.0.1:18083/health
curl http://127.0.0.1:18084/health
```

## 4. Ручная синхронизация

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

Очистка старых GPS-партиций:

```bash
docker compose -f docker-compose.server.yml run --rm gps-sync \
  python sync_arvento_gps_to_postgres.py retention
```

Массовая загрузка истории по умолчанию не выполняется. Дополнительные сутки загружаются только при реальной необходимости. Срок хранения задаётся через `ARVENTO_RETENTION_DAYS`.

## 5. Редактор геозон

Сервис слушает только localhost:

```text
http://127.0.0.1:18083
```

SSH-туннель:

```bash
ssh -L 18083:127.0.0.1:18083 root@SERVER_IP
```

Возможности:

- Google Satellite при наличии рабочего ключа;
- OpenStreetMap как обязательный fallback;
- точки, линии и полигоны;
- редактирование вершин;
- сохранение в PostGIS;
- версионирование геометрии;
- отключение зоны без удаления истории.

## 6. Портал отчётов

Сервис слушает только localhost:

```text
http://127.0.0.1:18084
```

Для временного доступа через SSH:

```bash
ssh -L 18084:127.0.0.1:18084 root@SERVER_IP
```

Для постоянного доступа используется Nginx, HTTPS и Basic Auth. Порт `18084` в UFW открывать не требуется.

Портал формирует:

- первый въезд через КПП;
- эффективность легкового транспорта;
- запрещённый поворот.

CSV и Excel создаются в системной временной папке контейнера и после ответа браузеру удаляются.

## 7. Обновление

```bash
cd /opt/arvento_report
git pull origin main
python verify_repository.py
docker compose -f docker-compose.server.yml build gps-sync geofence-editor report-portal
docker compose -f docker-compose.server.yml up -d --force-recreate gps-sync geofence-editor report-portal
```

Для изменения только портала:

```bash
cd /opt/arvento_report
git pull origin main
python verify_repository.py
docker compose -f docker-compose.server.yml build report-portal
docker compose -f docker-compose.server.yml up -d --force-recreate report-portal
```

## 8. Хранение

- GPS-точки: срок определяется `ARVENTO_RETENTION_DAYS`, используются суточные партиции PostgreSQL;
- геозоны и их версии: долгосрочно;
- повторные сообщения не дублируются;
- новые точки добавляют автомобиль и дату в `recalculation_queue`;
- временные файлы веб-портала не сохраняются в `reports`.

## 9. Резервное копирование

```bash
mkdir -p backups
docker compose -f docker-compose.server.yml exec -T postgres \
  pg_dump -U arvento_report -d arvento_report -Fc \
  > backups/arvento_report_$(date +%F).dump
```

Восстановление:

```bash
docker compose -f docker-compose.server.yml exec -T postgres \
  pg_restore -U arvento_report -d arvento_report --clean --if-exists \
  < backup.dump
```

## 10. Канонические исполняемые файлы

```text
sync_arvento_gps_to_postgres.py
run_geofence_editor.py
generate_kpp_summary_report.py
generate_first_entry_report.py
generate_prohibited_left_turn_report.py
generate_scheduled_reports.py
```

Старые имена оставлены только как совместимые внутренние реализации. В Docker, портале, новых командах и инструкциях они не используются.
