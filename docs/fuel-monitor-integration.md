# Интеграция заправок Fuel Monitor в сводный отчёт

Сводный отчёт получает правый столбец `Заправка, л`.

Источник данных:

```text
база fuel_monitor
таблица public.fuel_events
поля event_dt, plate, liters
```

Правило расчёта: сумма `liters` по календарной дате `event_dt` и нормализованному госномеру. Пробелы и разделители в госномере не учитываются. При настроенном подключении и отсутствии заправок за день в ячейку записывается `0.0`.

## 1. Создание пользователя только для чтения

На сервере PostgreSQL:

```bash
sudo -u postgres psql -d fuel_monitor
```

```sql
CREATE ROLE arvento_fuel_reader LOGIN PASSWORD 'CHANGE_ME_STRONG_PASSWORD';
GRANT CONNECT ON DATABASE fuel_monitor TO arvento_fuel_reader;
GRANT USAGE ON SCHEMA public TO arvento_fuel_reader;
GRANT SELECT ON TABLE public.fuel_events TO arvento_fuel_reader;
```

Выход:

```text
\q
```

## 2. Подключение портала

В `/opt/arvento_report/.env` добавить:

```env
FUEL_DATABASE_URL=postgresql://arvento_fuel_reader:CHANGE_ME_STRONG_PASSWORD@host.docker.internal:5432/fuel_monitor
FUEL_DB_CONNECT_TIMEOUT_SECONDS=10
```

Если пароль содержит специальные символы URL, их нужно URL-кодировать. Файл `.env` не добавлять в Git.

Docker Compose добавляет для портала адрес:

```text
host.docker.internal -> host-gateway
```

PostgreSQL должен принимать подключения с Docker bridge. Если подключение отклоняется, проверить `listen_addresses` и `pg_hba.conf`, разрешив только фактическую подсеть Docker и только пользователя `arvento_fuel_reader` для базы `fuel_monitor`.

## 3. Проверка подключения

```bash
cd /opt/arvento_report

docker compose -f docker-compose.server.yml run --rm --no-deps report-portal \
  python -c "import os, psycopg; c=psycopg.connect(os.environ['FUEL_DATABASE_URL'], connect_timeout=10); cur=c.cursor(); cur.execute('select count(*), max(event_dt) from public.fuel_events'); print(cur.fetchone()); c.close()"
```

Ожидается количество записей и время последней заправки.

## 4. Обновление только портала

```bash
cd /opt/arvento_report
git pull origin main
python verify_repository.py
docker compose -f docker-compose.server.yml build report-portal
docker compose -f docker-compose.server.yml up -d --force-recreate report-portal
docker compose -f docker-compose.server.yml logs --tail=100 report-portal
```

После этого сформировать сводный отчёт. В книге:

- справа появится столбец `Заправка, л`;
- на листе `Параметры` будет указан источник Fuel Monitor;
- данные будут агрегированы отдельно по каждой дате и каждому госномеру.

При пустом `FUEL_DATABASE_URL` отчёт продолжит формироваться, но столбец останется пустым.
