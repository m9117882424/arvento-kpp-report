# Инвентаризация и безопасная уборка Arvento на PRIMARY

Документ для сопровождения Arvento после перехода между чатами или операторами.
**Дата контрольной инвентаризации: 07.11.2026, Europe/Istanbul.**
Для актуальных значений всегда используйте команды ниже; цифры диска и количество
архивов изменяются со временем.

## Где что находится

| Ресурс | Назначение | Политика |
| --- | --- | --- |
| GitHub [`main`](https://github.com/m9117882424/arvento-kpp-report/tree/main) | Проверенный исходный код и документация | Единственный источник для новых релизов |
| `/opt/arvento_report` | Production checkout | `main`, без локальных правок и чужих файлов |
| `/opt/arvento_backups` | PostgreSQL custom-format `pg_dump` | Отдельно от Git; сохранять проверенные точки восстановления |
| `/var/lib/arvento-report/releases` | Состояния release и rollback | Не удалять без проверки текущего/предыдущего релиза |
| `/var/lib/arvento-report/legacy-untracked` | Архив одноразовых локальных файлов, если он создан | Доступ root, обратимое перемещение, не запускать оттуда production |
| `/etc/default/arvento-report`, `/opt/arvento_report/.env` | Рабочая конфигурация и секреты | Не копировать в Git или отчёт |
| `/usr/local/sbin/arvento-*` и `/etc/systemd/system/arvento-*` | Исполняемые скрипты и расписания | Сверять с `deploy/`, учитывать systemd drop-in |

## Контроль состояния перед уборкой

```bash
cd /opt/arvento_report
git status --short --branch
git rev-parse HEAD
git worktree list --porcelain
df -h /
df -i /
docker compose -f docker-compose.server.yml ps
systemctl list-timers --all --no-pager | grep 'arvento-'
sudo /usr/local/sbin/arvento-healthcheck
```

Не запускать несколько тяжёлых диагностик Docker параллельно. На этом VPS
работают и другие продукты (в том числе корпоративное такси); чистка Arvento
не разрешает удаление чужих образов, томов, данных или журналов.

## Git и временные worktree

Вспомогательные `git worktree` создавайте вне production checkout и удаляйте
командой `git worktree remove` **только после** проверки `git status --porcelain`
и достижимости commit из `main` или сохранённой архивной ветки. Для detached
commit, которого нет в `main`, сначала создайте локальную архивную ссылку:

```bash
git -C /opt/arvento_report branch archive/NAME COMMIT_SHA
git -C /opt/arvento_report worktree remove /opt/EXACT_TEST_WORKTREE
git -C /opt/arvento_report worktree prune --dry-run
```

Не применять `worktree remove --force` к грязной директории, не удалять
ветку с единственной копией изменений и не использовать `git clean -fd`.
Новые правки выполняются через отдельную ветку, PR, CI и fast-forward `main`.
Документация может обновляться на VPS без Docker-релиза; изменение исполняемого
кода требует регламентного выпуска.

## Локальные файлы вне Git

На момент инвентаризации в checkout оставались:

- `fuel_report_portal.py` — отдельная локальная заготовка отчёта по топливу
  от 21.09.2026; не импортируется основными production entrypoints;
- `migrations/` — локальные одноразовые файлы восстановления кэша за
  08.10.2026, включая инструкцию `README_20261009.md`.

Эти файлы **не являются мусором**: при очистке checkout их нужно переместить
с сохранением прав и контрольных сумм во внешний закрытый архив, а не удалить.
Путь архива и итог проверки записываются в журнал ниже. При необходимости
обратный перенос допустим только после проверки отсутствия одноимённых файлов
в checkout; runtime-операции и миграции вручную не запускать.

Архив от 07.11.2026:
`/var/lib/arvento-report/legacy-untracked/20261107-arvento-cleanup/`.
Из него можно вернуть исходные файлы обычным `cp -a` после проверки места
назначения. SHA-256 исходного и архивного содержимого совпали:

```text
fuel_report_portal.py                                  97b428f5c6e0417691fa6556c0ed61681b9423781175b523f9c83d4fefaa3bf7
migrations/20261009_invalidate_cache_after_roster_update.sql  2eadd78631dcb7b39bae4d4359c4a90a6846cb21b6b40744a14f155a5d0ca76e
migrations/20261009_reconcile_20261008_cache.py        d31d5d3c0b5531b85044b7436e25927ccf36ef0e4f01483f1aa303ff09b93bf6
migrations/README_20261009.md                          70fe63ef796127075b51a7f9fa14e5f28fda56bbe8bbbb00639ac860ee4573c0
```

## Backup и Docker

На PRIMARY штатный `arvento-backup.service` имеет локальный drop-in
`/etc/systemd/system/arvento-backup.service.d/20-retention-count.conf`,
запускающий `/usr/local/sbin/arvento-backup-retention /opt/arvento_backups 7`.
Основной versioned script выполняет `pg_dump -Fc`, `pg_restore --list` и
удаляет архивы старше 14 дней. Дополнительный скрипт ограничивает **общее
количество файлов**, а не количество различных календарных дней. Несколько
релизов за день способны уменьшить фактическую глубину истории; это известное
расхождение серверной настройки и Git-исходников, не повод удалять архивы
вручную. Перед правкой политики необходимо сохранить минимум одну проверенную
точку каждого нужного дня и возможность отката последнего релиза.

Для Docker удалять можно только **точно идентифицированные** устаревшие теги
`arvento-report`, после проверки `docker ps -a --format '{{.Image}}'`.
Текущий образ, предыдущий rollback, PostgreSQL/PostGIS, образы других
проектов, Docker volumes и новые/неизвестные локальные сборки сохранять.
`docker image prune -a`, `docker system prune --volumes` и ручную очистку
`/var/lib/docker` не использовать.

## Чек-лист после обслуживания

1. `git status --short` в `/opt/arvento_report` пуст; HEAD соответствует
   текущему `origin/main` (либо есть явно зафиксированный docs-only разрыв).
2. `docker compose ps`: PostgreSQL, редактор геозон, портал — `healthy`.
3. `/health` на портах `18083` и `18084` возвращает HTTP 200.
4. Все четыре timer проверены отдельно: intraday, nightly, backup, efficiency.
5. Последние GPS `sync_runs` свежие; после долгого `pg_dump` проверить,
   не завершился ли очередной intraday запуск `SKIP` из-за общего lock.
6. Последний backup существует и проходит `pg_restore --list`; для крупных
   изменений восстановление тестируется отдельным `restore-drill`.
7. В журнале ниже указаны точные действия, сохранённые/удалённые пути и
   возможность восстановления.

## Журнал обслуживания

| Дата | Итог | Восстановление |
| --- | --- | --- |
| 07.11.2026 20:42 | Промышленный release #34; проверен backup `arvento_report_20261107_202928.dump`; в 20:46:55 успешно завершены догоняющие GPS-sync и кэш | `deploy/release.sh` сохраняет предыдущий image и состояние rollback |
| 07.11.2026 после 21:00 | Убраны четыре чистых тестовых worktree из августа; три их commit входят в `main`, а отдельный commit `3696a206` сохранён ссылкой `archive/fleet-dashboard-test-20260815` | Worktree создаётся повторно через `git worktree add`; архивная ветка оставлена локально на PRIMARY |
| 07.11.2026 после 21:00 | Два непрошедших через Git артефакта перенесены из checkout в `legacy-untracked/20261107-arvento-cleanup`; SHA-256 совпали | Файлы и каталог на месте, доступ root; `cp -a` возвращает исходную структуру |
| 07.11.2026 после 21:00 | Удалены только 18 устаревших тегов/образов `arvento-report`, освобождено около 854 МБ | Immutable Git commit для удалённых `git-*` образов существует; можно пересобрать. Текущий и предыдущий rollback сохранены |

Дампы БД, Docker volumes, секреты, образы других проектов и системные журналы
в рамках этой уборки **не удалялись**.

Новые операции дополняйте конкретной датой, а не переписывайте историю.
