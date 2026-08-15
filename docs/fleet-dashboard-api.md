# API панели легкового автопарка

`report-portal` предоставляет отдельный read-only JSON API для панели автопарка. Он читает готовые суточные показатели Arvento и объединяет их с транзакциями Fuel Monitor по календарной дате и нормализованному госномеру.

API не запускает синхронизацию Arvento, не пересчитывает GPS-треки и не формирует Excel при запросе. Поэтому внешний сайт получает уже подготовленные показатели с предсказуемой нагрузкой на сервер.

## Источники и правила

| Показатель | Источник | Правило |
| --- | --- | --- |
| Пробег, работа, нарушения | `consolidated_report_cache` | готовая строка на дату и нормализованный госномер |
| Текущий статус и координаты | `vehicles` и последняя `gps_points` | `moving`, `parked` или `offline` по возрасту точки и скорости |
| Литры, стоимость, источник | Fuel Monitor `public.fuel_events` | сумма по дате, госномеру и `source` |
| Расход | оба контура | литры Fuel Monitor / пробег Arvento × 100 |
| Контроль качества | оба контура | разница live-суммы Fuel Monitor и `fuel_liters` в кэше, а также несопоставленные госномера |

Все автомобили Arvento считаются легковыми. Госномера сопоставляются без пробелов и разделителей, в верхнем регистре.

Если Fuel Monitor временно недоступен, Arvento-часть ответа остаётся доступной, `meta.fuel_status` становится `unavailable`, а топливные значения — `null`. Несопоставленные топливные операции не включаются в показатели автопарка и отдельно отражаются в `meta.unmatched_fuel_*`.

## Защита

Каждый endpoint требует заголовок `Authorization: Bearer <token>`. Секрет задаётся только в production `.env`:

```bash
openssl rand -hex 32
```

```env
FLEET_API_TOKEN=<отдельный случайный секрет>
```

При пустом `FLEET_API_TOKEN` API отвечает `503`. Токен нельзя добавлять в Git, JavaScript браузера или публичные переменные сайта. Внешний Sites-проект должен вызывать API только из серверного route/function и хранить токен в server secret.

Ответы получают `Cache-Control: private, no-store`, поскольку содержат актуальные координаты автомобилей. CORS намеренно не включён.

## Endpoints

### Проверка API

```text
GET /api/v1/fleet/health
```

Проверяет регистрацию защищённого API и показывает, настроен ли `FUEL_DATABASE_URL`. Это не запускает запросы в обе базы.

### Сводная панель

```text
GET /api/v1/fleet/dashboard?date_from=2026-08-01&date_to=2026-08-15
```

Ответ содержит:

- `meta` — период, актуальность кэша, состояние Fuel Monitor и показатели сопоставления;
- `summary` — количество машин и их статусы, пробег, литры, стоимость и расход;
- `daily` — дневной ряд пробега и топлива;
- `fuel_by_source` — разрез по Shell, Turpak и другим значениям `source`;
- `vehicles` — показатели и последнее состояние каждой машины.

### Карточка машины

```text
GET /api/v1/fleet/vehicles/01ABC123?date_from=2026-08-01&date_to=2026-08-15
```

Возвращает итог машины, дневной ряд и до 500 последних топливных операций за выбранный период. Номера карт, чеков и внутренние идентификаторы Fuel Monitor не выдаются.

Период включительный и по умолчанию ограничен 93 днями.

## Переменные окружения

```env
FLEET_API_TOKEN=
FLEET_API_MAX_PERIOD_DAYS=93
FLEET_API_STATEMENT_TIMEOUT_MS=15000
FLEET_API_OFFLINE_MINUTES=180
FLEET_API_MOVING_SPEED_KMH=3
```

- `FLEET_API_STATEMENT_TIMEOUT_MS` ограничивает каждый SQL-запрос;
- `FLEET_API_OFFLINE_MINUTES` определяет возраст точки, после которого машина считается недоступной;
- `FLEET_API_MOVING_SPEED_KMH` отделяет движение от стоянки.

Подключение к Fuel Monitor остаётся read-only и настраивается согласно [`fuel-monitor-integration.md`](fuel-monitor-integration.md).

## Nginx

Портал сохраняет Basic Auth для пользовательского интерфейса, но machine-to-machine path должен пропускать заголовок Bearer без окна браузерной авторизации. В `server`-блоке до общего `location /` используется конфигурация из [`deploy/nginx/arvento-report.conf.example`](../deploy/nginx/arvento-report.conf.example):

```nginx
location ^~ /api/v1/fleet/ {
    auth_basic off;
    proxy_pass http://127.0.0.1:18084;
    # стандартные X-Forwarded-* headers и конечные timeout
}
```

`auth_basic off` допустим только для этого префикса: доступ всё равно блокируется Bearer-проверкой приложения. Сам порт `18084` остаётся привязан к localhost.

После изменения:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

## Развёртывание и проверка

В production `.env` заполнить `FLEET_API_TOKEN`, затем пересобрать только портал:

```bash
cd /opt/arvento_report
python3 verify_repository.py
python3 verify_deployment.py
docker compose -f docker-compose.server.yml build report-portal
docker compose -f docker-compose.server.yml up -d --force-recreate report-portal
```

Локальная проверка через loopback:

```bash
curl -fsS \
  -H "Authorization: Bearer $FLEET_API_TOKEN" \
  http://127.0.0.1:18084/api/v1/fleet/health

curl -fsS \
  -H "Authorization: Bearer $FLEET_API_TOKEN" \
  "http://127.0.0.1:18084/api/v1/fleet/dashboard?date_from=2026-08-01&date_to=2026-08-15"
```

Для Sites задать два серверных секрета:

```text
ARVENTO_API_URL=https://reports.example.com/api/v1/fleet
ARVENTO_API_TOKEN=<то же значение FLEET_API_TOKEN>
```

Серверный route Sites добавляет Bearer-заголовок, передаёт `date_from`/`date_to` и возвращает клиенту только JSON ответа. Прямой вызов Arvento API или Fuel Monitor из браузера не требуется.
