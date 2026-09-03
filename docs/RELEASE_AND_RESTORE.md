# Безопасный release, rollback и проверка восстановления

## Канонический release

Перед началом checkout должен находиться на проверенном commit и не содержать
локальных изменений:

```bash
cd /opt/arvento_report
git fetch origin
git pull --ff-only origin main
sudo bash deploy/release.sh /opt/arvento_report
```

Release выполняет полный цикл без ручного переключения между шагами:

1. блокирует параллельный release;
2. создаёт и проверяет свежий PostgreSQL backup;
3. запускает production healthcheck;
4. сохраняет текущий image и host-side scripts для rollback;
5. останавливает timers;
6. собирает image `arvento-report:git-<COMMIT_SHA>` с OCI revision label;
7. устанавливает scripts и systemd units;
8. включает timers и выполняет post-deploy smoke-test;
9. сохраняет состояние release в `/var/lib/arvento-report/releases`.

При ошибке установки или smoke-test скрипт автоматически возвращает сохранённый
image и host-side scripts, включает timers и проверяет восстановленный контур.
PostgreSQL-контейнер и его volume при rollback не пересоздаются.

## Release checklist

- pull request прошёл `repository-verification`;
- production checkout чистый и указывает на ожидаемый commit;
- перед release нет активной ручной синхронизации или генерации отчёта;
- автоматический backup и preflight healthcheck прошли;
- в выводе есть `POST_DEPLOY_SMOKE OK` и `RELEASE OK`;
- три systemd timer имеют состояния `active` и `enabled`;
- после release сформирован один небольшой отчёт из готового кэша;
- следующий внутридневной pipeline завершился `SUCCESS`.

## Restore drill

Restore drill проверяет не только структуру архива, но и реальное восстановление.
Он создаёт отдельную временную БД с безопасным префиксом, восстанавливает туда
последний backup, проверяет таблицы и историю `sync_runs`, затем удаляет только
эту временную БД. Production database не изменяется.

Последний backup:

```bash
sudo /usr/local/sbin/arvento-restore-drill
```

Конкретный backup:

```bash
sudo /usr/local/sbin/arvento-restore-drill \
  /opt/arvento_backups/arvento_report_YYYYMMDD_HHMMSS.dump
```

Успешный результат заканчивается строкой `RESTORE_DRILL OK`. Во время проверки
используется общий pipeline lock, поэтому синхронизация и backup не работают
параллельно с восстановлением.

## Журнал проверок восстановления

| Дата | Backup | Результат | Длительность | Контрольные данные |
| --- | --- | --- | --- | --- |
| 03.09.2026 | `arvento_report_20260903_160150.dump` | успешно, код `0` | 14 мин 32 с | 85 таблиц, 1619 записей `sync_runs` |

Первая production-проверка восстановила backup в отдельную временную БД и
завершила её автоматическим удалением. Production database не изменялась.
