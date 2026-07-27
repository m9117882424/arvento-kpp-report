# Развёртывание Arvento PostgreSQL на сервере

## 1. Подготовка

```bash
git clone https://github.com/m9117882424/arvento-kpp-report.git
cd arvento-kpp-report
cp .env.server.example .env
nano .env
```

Заполнить обязательные параметры:

```text
POSTGRES_PASSWORD
ARVENTO_USER
ARVENTO_PIN1
ARVENTO_PIN2
ARVENTO_GROUP
```

Для редактора геозон при наличии ключа Google:

```text
GOOGLE_MAPS_API_KEY
DEFAULT_MAP_PROVIDER=google
OSM_FALLBACK_ENABLED=true
```

Если `GOOGLE_MAPS_API_KEY` пустой либо Google Maps не загрузился, редактор автоматически использует OpenStreetMap.

Не добавлять `.env` в Git.

## 2. Запуск

```bash
docker compose -f docker-compose.server.yml up -d --build
```

Проверка:

```bash
docker compose -f docker-compose.server.yml ps
docker compose -f docker-compose.server.yml logs -f sync
docker compose -f docker-compose.server.yml logs -f geofence-editor
```

Редактор слушает только localhost:

```text
http://127.0.0.1:18083
```

Для временной проверки через SSH-туннель:

```bash
ssh -L 18083:127.0.0.1:18083 root@SERVER_IP
```

Затем открыть локально:

```text
http://127.0.0.1:18083
```

Для постоянного доступа опубликовать редактор через Nginx и HTTPS, не открывая порт PostgreSQL.

## 3. Первичная загрузка истории за 60 дней

Контейнер синхронизации в штатном режиме загружает последние 6 часов каждые 30 минут.
Для первоначального заполнения выполнять дни последовательно:

```bash
for i in $(seq 60 -1 1); do
  D=$(date -d "$i days ago" +%F)
  docker compose -f docker-compose.server.yml run --rm sync \
    python arvento_postgres_sync_v2.py day "$D"
done
```

После первичной загрузки штатный контейнер продолжит дозагрузку автоматически.

## 4. Ручные команды

Последние 6 часов:

```bash
docker compose -f docker-compose.server.yml run --rm sync \
  python arvento_postgres_sync_v2.py recent --hours 6
```

Конкретные сутки:

```bash
docker compose -f docker-compose.server.yml run --rm sync \
  python arvento_postgres_sync_v2.py day 2026-07-24
```

Очистка старых GPS-партиций:

```bash
docker compose -f docker-compose.server.yml run --rm sync \
  python arvento_postgres_sync_v2.py retention
```

## 5. Редактор геозон

Возможности первой версии:

- OpenStreetMap как обязательная резервная подложка;
- Google Satellite при наличии рабочего API-ключа;
- автоматическое переключение на OSM при отсутствии или ошибке ключа;
- рисование точки, линии или полигона;
- редактирование вершин;
- сохранение в PostGIS;
- создание новой версии при изменении геометрии;
- отключение геозоны без удаления истории.

Проверка API:

```bash
curl http://127.0.0.1:18083/health
curl http://127.0.0.1:18083/api/config
```

Таблицы `geofences` и `geofence_versions` создаются редактором при первом запуске.

## 6. Хранение

- GPS-точки: 60 дней, суточные партиции PostgreSQL.
- Въезды, нарушения и суточные показатели: долгосрочное хранение.
- Повторно загруженные сообщения не дублируются.
- Новые точки добавляют автомобиль и дату в `recalculation_queue`.
- Геозоны и все их версии хранятся долгосрочно.

## 7. Резервное копирование

```bash
mkdir -p backups
docker compose -f docker-compose.server.yml exec -T postgres \
  pg_dump -U arvento -d arvento -Fc > backups/arvento_$(date +%F).dump
```

Восстановление:

```bash
docker compose -f docker-compose.server.yml exec -T postgres \
  pg_restore -U arvento -d arvento --clean --if-exists < backup.dump
```

## 8. Текущий статус

На данном этапе реализованы:

- PostgreSQL + PostGIS;
- суточные партиции GPS;
- регулярная загрузка последних 6 часов;
- разбиение API-запросов на двухчасовые интервалы;
- защита от дублей;
- журнал запусков и чанков;
- очередь пересчёта по автомобилю и дате;
- автоудаление GPS старше 60 дней;
- получение названия геозоны Arvento как справочного поля;
- веб-редактор собственных геозон с Google/OSM fallback.

Расчётные worker-модули для въездов, нарушений, пробега, простоев и эффективности будут подключаться к `recalculation_queue` отдельным этапом.
