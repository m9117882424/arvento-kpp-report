# База готовых данных сводного отчёта

## Назначение

Готовые строки сводного отчёта сохраняются в PostgreSQL. Исторический запрос читает их напрямую и не пересчитывает GPS-треки.

Основные таблицы:

- `consolidated_report_cache` — одна итоговая строка на дату и нормализованный госномер;
- `consolidated_cache_days` — состояние и время обновления каждой даты;
- `consolidated_cache_runs` — журнал автоматических и ручных запусков;
- `consolidated_roster_snapshots` и `consolidated_roster_entries` — сохранённые датированные разнарядки;
- `recalculation_queue` — автомобили и даты, по которым после синхронизации появились новые GPS-точки.

Схема создаётся версионированными миграциями при запуске портала. Применённые
версии и их контрольные суммы хранятся в `schema_migrations`.

## Актуальное расписание

Отдельный scheduler кэша больше не используется. Расчёт кэша является второй стадией общей цепочки синхронизации:

```text
arvento-intraday-pipeline.timer   каждые 30 минут с 01:00 до 23:30 Europe/Istanbul
arvento-nightly-correction.timer  ежедневно в 00:10 Europe/Istanbul
```

Внутридневная цепочка:

1. загружает последний час Arvento; получасовое перекрытие защищает от запаздывающих точек;
2. проверяет новую запись в `sync_runs`;
3. выбирает из `recalculation_queue` только автомобили, для которых реально добавлены новые GPS-точки;
4. для каждого выбранного автомобиля пересчитывает итоговую строку текущей даты по полному треку от 00:00;
5. атомарно заменяет только затронутые строки и помечает элементы очереди выполненными.

Перекрывающиеся интервалы синхронизации не создают дубли: GPS-точки защищены уникальным ключом `(source_hash, event_time)`.

Ночная цепочка не изменилась: она загружает предыдущие календарные сутки целиком, выполняет полный пересчёт всех автомобилей за этот день и закрывает очередь пересчёта этой даты. Это исправляет поздно поступившие точки и формирует окончательные суточные показатели.

Ключ итоговой строки: `(report_day, normalized_plate)`. Дубли не создаются.

## Почему внутридневной кэш считается не за последний час

Сводный кэш хранит суточные итоги. Пробег, проценты, первое прибытие, последнее убытие и отработанное время нельзя корректно складывать из перекрывающихся часовых интервалов. Поэтому час ограничивает только получение данных из Arvento. Для затронутого автомобиля расчёт всегда использует полный доступный трек текущих суток.

Это уменьшает нагрузку без потери корректности: вместо полного пересчёта всех автомобилей каждые 30 минут обновляется только изменившаяся часть суточного кэша.

## Первичное заполнение разнарядок

После установки откройте страницу управления разнарядками и загрузите датированные
файлы через центральный реестр. Затем выполните полный расчёт нужного периода. Система:

1. сохранит разнарядки в PostgreSQL;
2. для каждого дня выберет точную либо последнюю предыдущую разнарядку;
3. выполнит расчёт и сохранит готовые строки в историю.

Будущая разнарядка задним числом не применяется. Если на дату нет ни точной, ни
предыдущей разнарядки, расчёт завершается явной ошибкой. После первичного расчёта
исторический отчёт можно запрашивать без повторной загрузки файлов.

Кэш считается актуальным только при совпадении контрольных отметок GPS,
VehicleDistanceReport, разнарядки, геозон и версии алгоритма. После изменения
любого из этих источников день автоматически требует пересчёта.

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

Инкрементальный расчёт очереди за текущий день:

```bash
cd /opt/arvento_report
flock -n /run/arvento-consolidated-cache.lock \
  timeout --signal=TERM --kill-after=60 3600 \
  docker compose -f docker-compose.server.yml run --rm --no-deps report-portal \
  python consolidated_cache_worker.py refresh-pending \
    --date 2026-08-04 \
    --trigger manual-pending
```

Полный расчёт одного дня:

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

Очередь пересчёта:

```bash
docker exec arvento_report-postgres-1 sh -lc '
psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off -c "
SELECT day,
       count(*) FILTER (WHERE completed_at IS NULL) AS pending,
       count(*) FILTER (WHERE completed_at IS NOT NULL) AS completed
FROM recalculation_queue
GROUP BY day
ORDER BY day DESC
LIMIT 10;
"'
```

Состояние кэша:

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
- полный ночной расчёт заменяет календарный день целиком;
- внутридневной расчёт атомарно заменяет только строки автомобилей из очереди.
