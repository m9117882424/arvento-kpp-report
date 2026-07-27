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

Для Google Satellite дополнительно указывается `GOOGLE_MAPS_API_KEY`. При пустом или нерабочем ключе редактор автоматически использует OpenStreetMap.

Файл `.env` не добавлять в Git.

## 2. Проверка конфигурации

```bash
docker compose -f docker-compose.server.yml config
```

Compose-проект имеет имя `arvento_report`. Сервисы:

```text
postgres
gps-sync
geofence-editor
```

## 3. Запуск

```bash
docker compose -f docker-compose.server.yml up -d --build
```

Проверка:

```bash
docker compose -f docker-compose.server.yml ps
docker compose -f docker-compose.server.yml logs -f gps-sync
docker compose -f docker-compose.server.yml logs -f geofence-editor
curl http://127.0.0.1:18083/health
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

## 5. Первичная загрузка истории

Сначала загрузить 3–7 дней и измерить размер базы:

```bash
for i in $(seq 7 -1 1); do
  D=$(date -d "$i days ago" +%F)
  docker compose -f docker-compose.server.yml run --rm gps-sync \
    python sync_arvento_gps_to_postgres.py day "$D" || break
  sleep 10
done
```

После проверки диска загрузить полный период хранения:

```bash
for i in $(seq 60 -1 1); do
  D=$(date -d "$i days ago" +%F)
  docker compose -f docker-compose.server.yml run --rm gps-sync \
    python sync_arvento_gps_to_postgres.py day "$D" || break
  sleep 10
done
```

## 6. Редактор геозон

Сервис слушает только localhost:

```text
http://127.0.0.1:18083
```

SSH-туннель:

```bash
ssh -L 18083:127.0.0.1:18083 root@SERVER_IP
```

После этого открыть локально:

```text
http://127.0.0.1:18083
```

Возможности:

- Google Satellite при наличии рабочего ключа;
- OpenStreetMap как обязательный fallback;
- точки, линии, полигоны;
- редактирование вершин;
- сохранение в PostGIS;
- версионирование геометрии;
- отключение зоны без удаления истории.

## 7. Хранение

- GPS-точки: 60 дней, суточные партиции PostgreSQL;
- геозоны и их версии: долгосрочно;
- въезды, нарушения и суточные показатели: долгосрочно;
- повторные сообщения не дублируются;
- новые точки добавляют автомобиль и дату в `recalculation_queue`.

## 8. Резервное копирование

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

## 9. Канонические исполняемые файлы

```text
sync_arvento_gps_to_postgres.py
run_geofence_editor.py
generate_kpp_summary_report.py
generate_first_entry_report.py
generate_prohibited_left_turn_report.py
generate_scheduled_reports.py
```

Старые имена оставлены только как совместимые внутренние реализации. В новых командах и инструкциях они не используются.
