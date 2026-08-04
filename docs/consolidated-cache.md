# База готовых данных сводного отчёта

## Назначение

Готовые строки сводного отчёта сохраняются в PostgreSQL. Исторический запрос читает их напрямую и не пересчитывает GPS-треки.

Основные таблицы:

- `consolidated_report_cache` — одна итоговая строка на дату и нормализованный госномер;
- `consolidated_cache_days` — состояние и время обновления каждой даты;
- `consolidated_cache_runs` — журнал автоматических и ручных запусков;
- `consolidated_roster_snapshots` и `consolidated_roster_entries` — сохранённые датированные разнарядки.

Схема создаётся автоматически при первом обращении.

## Актуальное расписание

Отдельный `arvento-consolidated-cache.timer` больше не используется. Расчёт кэша является второй стадией общей цепочки синхронизации:

```text
arvento-intraday-pipeline.timer   каждые 30 минут с 05:00 до 23:30 Europe/Istanbul
arvento-nightly-correction.timer  ежедневно в 00:10 Europe/Istanbul
```

Внутридневная цепочка:

1. загружает последние шесть часов Arvento;
2. проверяет новую запись в `sync_runs`;
3. только после статуса `SUCCESS` пересчитывает текущую дату.

Ночная цепочка загружает и пересчитывает предыдущие календарные сутки. Поздно поступившие GPS-точки попадают в ночную корректировку либо следующий внутридневный запуск.

Ключ итоговой строки: `(report_day, normalized_plate)`. Дубли не создаются.

## Первичное заполнение разнарядок

После установки один раз сформируйте сводный отчёт с загруженными разнарядками. Портал:

1. сохранит разнарядки в PostgreSQL;
2. выполнит расчёт;
3. сохранит готовые строки в историю.

После этого исторический отчёт можно запрашивать без повторной загрузки разнарядок. Если готовых данных за весь период нет, портал потребует разнарядку или ручную дозагрузку.

## Установка расписания

Расписание устанавливается общим installer:

```bash
cd /opt/arvento_report
sudo bash deploy/install.sh /opt/arvento_report
```

Проверка:

```bash
systemctl list-timers --all --no-pager | grep -E 'arvento-(intraday|nightly)'
systemctl status arvento-intraday-pipeline.timer --no-pager -l
systemctl status arvento-nightly-correction.timer --no-pager -l
```

## Ручной расчёт

Перед ручным расчётом убедитесь, что другая цепочка не выполняется. Используйте тот же lock.

Один день:

```bash
cd /opt/arvento_report
flock -n /run/arvento-consolidated-cache.lock \
  timeout --signal=TERM --kill-after=60 3600 \
  docker compose -f docker-compose.server.yml run --rm --no-deps report-portal \
  python consolidated_cache_worker.py refresh \
    --date 2026-07-28 \
    --trigger manual
```

Произвольный период до 31 дня:

```bash
cd /opt/arvento_report
flock -n /run/arvento-consolidated-cache.lock \
  timeout --signal=TERM --kill-after=60 7200 \
  docker compose -f docker-compose.server.yml run --rm --no-deps report-portal \
  python consolidated_cache_worker.py refresh \
    --date-from 2026-07-01 \
    --date-to 2026-07-28 \
    --trigger backfill
```

Массовый backfill выполняйте только в отдельное окно после оценки нагрузки. Для проверки новой логики предпочтительна отдельная тестовая база.

## Контроль

```bash
cd /opt/arvento_report
docker compose -f docker-compose.server.yml run --rm --no-deps report-portal \
  python consolidated_cache_worker.py status --limit 10
```

Журналы:

```bash
journalctl -u arvento-intraday-pipeline.service -n 200 --no-pager
journalctl -u arvento-nightly-correction.service -n 200 --no-pager
```

SQL-проверка:

```bash
docker exec arvento_report-postgres-1 sh -lc '
psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off -c "
SELECT report_day, status, row_count,
       gps_max_event_time AT TIME ZONE '\''Europe/Istanbul'\'' AS gps_max_tr,
       refreshed_at AT TIME ZONE '\''Europe/Istanbul'\'' AS refreshed_tr
FROM consolidated_cache_days
ORDER BY report_day DESC
LIMIT 10;
"'
```

## Защита от параллельных запусков

- вся цепочка синхронизации использует `/run/arvento-sync-and-cache.lock`;
- стадия кэша использует `/run/arvento-consolidated-cache.lock`;
- worker дополнительно использует PostgreSQL advisory lock;
- каждый этап имеет конечный timeout;
- если sync завершён не со статусом `SUCCESS`, кэш не запускается;
- готовые строки даты заменяются только внутри успешной транзакции.
