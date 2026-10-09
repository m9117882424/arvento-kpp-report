# Ночной пересчёт Arvento: быстрый итоговый кэш

Дата внедрения: **09.10.2026**. Обслуживаемая система: Arvento Report на общем VPS. Код размещён в `deploy/arvento-fast-nightly-cache.py`, а вызов добавлен в `deploy/arvento-sync-and-cache.sh` и установлен в `/usr/local/sbin/arvento-sync-and-cache`.

## Проблема и исправление

Прежний ночной процесс после успешной синхронизации GPS и VehicleDistanceReport вызывал полный генератор XLSX, который 09.10.2026 достиг лимита 3600 секунд и завершился `rc=124`. Данные GPS и пробега были загружены корректно; отказ относился только к полному пересчёту сводного кэша.

Ночной процесс теперь использует проверенный алгоритм инкрементального анализа GPS **для всех автомобилей дня**, а не только очереди новых GPS-событий. Для машин из выбранной разнарядки, у которых нет пригодного GPS-трека, создаются строки с нулевым пробегом, сохраняя карточку автомобиля в отчёте. Результат атомарно записывается через `upsert_incremental_rows()`, включая обновление календарного кэша, контроль завершения очереди и кандидатов пересмотра пробега. По-прежнему применяются геозоны, бизнес-правила, топливо, политика пробега, исходная дата и разнарядка.

Блокировки: штатный `/run/arvento-sync-and-cache.lock`, `/run/arvento-consolidated-cache.lock` и PostgreSQL advisory lock, общий с обычным cache-worker. Если GPS или разнарядка изменились во время расчёта либо набор итоговых ключей отличается, процесс завершается ошибкой, не выдавая неполный расчёт за успех. Таймаут этапа — `PIPELINE_NIGHTLY_FAST_CACHE_TIMEOUT_SECONDS` (по умолчанию 1200 секунд), без изменения лимитов загрузки GPS.

Переход не требует перезапуска Docker Compose, PostgreSQL или веб-портала: отдельный контейнер получает только этот скрипт через read-only volume. Получасовой intraday-процесс не изменён.

## Результат контрольного запуска

Вечером 09.10.2026 за 65,9 секунды рассчитаны 205 машин вместо прежнего кэша на 199 машин. В исходной разнарядке — 205 машин, GPS — 199, без GPS — 6. Итог: 205 строк; все `in_roster=true`; `consolidated_cache_days.status=SUCCESS`; cache run 3240. Независимая проверка таблиц, количества машин и блокировок пройдена.

Перед изменением выполнен приватный PostgreSQL logical backup четырёх таблиц кэша, проверено 4 archive TABLE DATA entry и SHA256. Обычная полная backup Arvento от 09.10.2026 03:30 сохранилась без изменений.

## Проверка следующего ночного цикла

Расписание: 00:10 Europe/Istanbul; исходные источники: Arvento GPS sync → VehicleDistanceReport → быстрый кэш.

```bash
systemctl status arvento-nightly-correction.service
journalctl -u arvento-nightly-correction.service -n 50 --no-pager
systemctl list-timers --all | grep arvento
```

Успешный журнал должен содержать `SYNC_RESULT status=SUCCESS`, `DISTANCE status=SUCCESS`, `CACHE START_FAST_DAY`, `CACHE SUCCESS` и `SUCCESS: загрузка, VehicleDistanceReport и расчёт завершены`. `row_count` в `consolidated_cache_days` должен покрывать все автомобили эффективной разнарядки с учётом GPS.

Ручная **проверка только чтения**, без скачивания GPS:

```bash
docker compose --project-directory /opt/arvento_report \
  -f /opt/arvento_report/docker-compose.server.yml run --rm --no-deps \
  -v /opt/arvento_report/deploy/arvento-fast-nightly-cache.py:/tmp/arvento-fast-nightly-cache.py:ro \
  report-portal python -B /tmp/arvento-fast-nightly-cache.py \
  --date YYYY-MM-DD --check-only
```

## Возврат

Исходные файлы `before-repo.sh` и `before-system.sh` с контрольными суммами сохранены под `/opt/arvento_backups/ops-fast-nightly-20261009` (root-only 0700). При подтверждённой регрессии остановить выполнение по завершении активного задания, восстановить установленный скрипт **только из проверенной резервной копии** и сохранить журнал. Автоматически удалять кэш, перезаписывать GPS/разнарядки или выполнять destructive SQL нельзя.

Полный медленный генератор XLSX сохранён в исходном коде и доступен для ручной диагностики, но более не запускается каждый вечер. Отдельный запуск полного генератора на больших данных может снова превысить 3600 секунд.

Общее обслуживание VPS (ядро, Docker/Containerd, Netplan) требует согласованного окна, так как сервер обслуживает также Taxi, Traccar, Metabase и другие проекты. Общий reboot и Docker restart этим исправлением **не выполнялись**.
