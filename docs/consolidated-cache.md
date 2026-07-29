# База готовых данных сводного отчёта

## Назначение

Готовые строки сводного отчёта сохраняются в PostgreSQL. Исторический запрос читает их напрямую и не пересчитывает GPS-треки.

Основные таблицы:

- `consolidated_report_cache` — одна итоговая строка на дату и нормализованный госномер;
- `consolidated_cache_days` — состояние и время обновления каждой даты;
- `consolidated_cache_runs` — журнал автоматических и ручных запусков;
- `consolidated_roster_snapshots` и `consolidated_roster_entries` — сохранённые датированные разнарядки.

Схема создаётся автоматически при первом обращении.

## Логика наложения

Плановый запуск выполняется три раза в день по `Europe/Istanbul`:

- 08:00;
- 12:30;
- 20:00.

Каждый запуск полностью пересчитывает и атомарно заменяет два календарных дня:

- вчера;
- сегодня.

Поэтому одни и те же даты перекрываются несколькими запусками. Поздно поступившие GPS-точки и заправки попадают в следующий запуск. Участок текущего дня после 20:00 окончательно догружается на следующий день в 08:00, когда эта дата пересчитывается как «вчера».

Ключ строки: `(report_day, normalized_plate)`. Дубли не создаются.

## Первичное заполнение разнарядок

После обновления портала один раз сформируйте сводный отчёт с загруженными разнарядками. Портал:

1. сохранит разнарядки в PostgreSQL;
2. выполнит обычный расчёт;
3. сохранит готовые строки в историю.

После этого исторический отчёт можно запрашивать без повторной загрузки разнарядок. Если готовых данных за весь период нет, портал потребует разнарядку или ручную дозагрузку.

## Установка таймера

```bash
cd /opt/arvento_report
sudo cp deploy/systemd/arvento-consolidated-cache.service /etc/systemd/system/
sudo cp deploy/systemd/arvento-consolidated-cache.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now arvento-consolidated-cache.timer
```

Проверка расписания:

```bash
systemctl list-timers --all arvento-consolidated-cache.timer
systemctl status arvento-consolidated-cache.timer --no-pager -l
```

## Ручной первый запуск

Обновить вчера и сегодня:

```bash
cd /opt/arvento_report
docker compose -f docker-compose.server.yml run --rm --no-deps report-portal \
  python consolidated_cache_worker.py refresh --days-back 1 --trigger initial
```

Один день:

```bash
docker compose -f docker-compose.server.yml run --rm --no-deps report-portal \
  python consolidated_cache_worker.py refresh --date 2026-07-28 --trigger manual
```

Произвольный период до 31 дня:

```bash
docker compose -f docker-compose.server.yml run --rm --no-deps report-portal \
  python consolidated_cache_worker.py refresh \
  --date-from 2026-07-01 --date-to 2026-07-28 --trigger backfill
```

## Контроль

```bash
cd /opt/arvento_report
docker compose -f docker-compose.server.yml run --rm --no-deps report-portal \
  python consolidated_cache_worker.py status --limit 10
```

Журнал systemd:

```bash
journalctl -u arvento-consolidated-cache.service -n 200 --no-pager
```

SQL-проверка:

```bash
docker exec arvento_report-postgres-1 sh -lc '
psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT report_day, status, row_count,
       gps_max_event_time AT TIME ZONE '\''Europe/Istanbul'\'' AS gps_max_tr,
       refreshed_at AT TIME ZONE '\''Europe/Istanbul'\'' AS refreshed_tr
FROM consolidated_cache_days
ORDER BY report_day DESC
LIMIT 10;
"'
```

## Параллельные запуски

- systemd использует `flock`;
- worker дополнительно использует PostgreSQL advisory lock;
- если предыдущий расчёт ещё выполняется, новый запуск завершается со статусом `SKIPPED`;
- готовые строки даты заменяются только внутри успешной транзакции.
