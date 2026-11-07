# Безопасный release, rollback и проверка восстановления

## Канонический release

Перед началом checkout должен находиться на проверенном commit и не содержать
локальных изменений, включая неотслеживаемые файлы. Исторические локальные
скрипты предварительно сохраняются **вне** checkout (см.
[`SERVER_HYGIENE.md`](SERVER_HYGIENE.md)); не используйте `git clean -fd`:

```bash
cd /opt/arvento_report
git status --short
git switch main
git fetch origin
git merge --ff-only origin/main
sudo bash deploy/release.sh /opt/arvento_report
```

Если PR затронул **только Markdown-документацию**, достаточно fast-forward
checkout без запуска `deploy/release.sh`: повторный много-гигабайтный `pg_dump`
и пересборка образа не нужны. В таком случае Git HEAD отражает обновлённую
документацию, а запущенный image сохраняет commit предыдущего **кода**;
это контролируемое docs-only различие, а не самовольный patch production.

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
- три обязательных systemd timer (intraday, nightly, backup) имеют состояния
  `active` и `enabled`; дополнительный прогрев эффективности
  `arvento-efficiency-cache.timer` на PRIMARY тоже активен, но устанавливается
  как опциональный и не считается причиной отката core release;
- после release сформирован один небольшой отчёт из готового кэша;
- следующий внутридневной pipeline завершился `SUCCESS`. Если долгий
  предрелизный backup занял интервалы планировщика и запуски отчитались `SKIP`
  из-за общего lock, после снятия lock выполняют один штатный догоняющий
  запуск и проверяют обновление кэша.

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

## Последний контрольный release

07.11.2026 20:42 (Europe/Istanbul) опубликован commit
`a9da41025103f604cb2cba50fd1c22b75e09406c` (PR #34).
Контрольный backup `arvento_report_20261107_202928.dump` размером 3,9 ГБ
прошёл проверку структуры `pg_restore --list`; это **не** полноценный
restore drill. Post-deploy smoke завершился `OK`, все три контейнера `healthy`.
В 20:46:55 завершилась догоняющая GPS-синхронизация, рассчитано и сохранено
в кэш 189 автомобилей. Состояние и процедуру уборки 07.11 фиксирует
[`SERVER_HYGIENE.md`](SERVER_HYGIENE.md).
