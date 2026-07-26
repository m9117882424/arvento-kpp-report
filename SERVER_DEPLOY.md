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

Не добавлять `.env` в Git.

## 2. Запуск

```bash
docker compose -f docker-compose.server.yml up -d --build
```

Проверка:

```bash
docker compose -f docker-compose.server.yml ps
docker compose -f docker-compose.server.yml logs -f sync
```

## 3. Первичная загрузка истории за 60 дней

Контейнер синхронизации в штатном режиме загружает последние 6 часов каждые 30 минут.
Для первоначального заполнения выполнять дни последовательно:

```bash
for i in $(seq 60 -1 1); do
  D=$(date -d "$i days ago" +%F)
  docker compose -f docker-compose.server.yml run --rm sync \
    python arvento_postgres_sync.py day "$D"
done
```

После первичной загрузки штатный контейнер продолжит дозагрузку автоматически.

## 4. Ручные команды

Последние 6 часов:

```bash
docker compose -f docker-compose.server.yml run --rm sync \
  python arvento_postgres_sync.py recent --hours 6
```

Конкретные сутки:

```bash
docker compose -f docker-compose.server.yml run --rm sync \
  python arvento_postgres_sync.py day 2026-07-24
```

Очистка старых GPS-партиций:

```bash
docker compose -f docker-compose.server.yml run --rm sync \
  python arvento_postgres_sync.py retention
```

## 5. Хранение

- GPS-точки: 60 дней, суточные партиции PostgreSQL.
- Въезды, нарушения и суточные показатели: долгосрочное хранение.
- Повторно загруженные сообщения не дублируются.
- Новые точки добавляют автомобиль и дату в `recalculation_queue`.

## 6. Резервное копирование

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

## 7. Текущий статус

На данном этапе реализованы:

- PostgreSQL + PostGIS;
- суточные партиции GPS;
- регулярная загрузка последних 6 часов;
- разбиение API-запросов на двухчасовые интервалы;
- защита от дублей;
- журнал запусков и чанков;
- очередь пересчёта по автомобилю и дате;
- автоудаление GPS старше 60 дней.

Расчётные worker-модули для въездов, нарушений, пробега, простоев и эффективности будут подключаться к `recalculation_queue` отдельным этапом.
