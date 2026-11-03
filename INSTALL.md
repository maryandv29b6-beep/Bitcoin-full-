# FlowLens: установка и запуск в реальной среде

Инструкция для того, кто будет разворачивать проект. Подробности о функциях —
в `README.md`, формулы — в `docs/FORMULAS.md`, устройство — в
`docs/ARCHITECTURE.md`.

## 1. Что в архиве

```
FlowLens/
  INSTALL.md            эта инструкция
  README.md             описание, интерфейс, ограничения источников
  run.bat / run.sh      запуск одной командой (Windows / Linux, macOS)
  Dockerfile, docker-compose.yml
  config/flowlens.config.example.json   образец настроек
  backend/              сервер на Python: сбор данных, хранение, API
    flowlens/static/    уже собранный веб-интерфейс (Node.js не нужен)
    tests/              34 автотеста
    tools/              нагрузочный тест, выгрузка статичного снимка
  frontend/             исходники интерфейса (React + TypeScript)
  docs/                 формулы, архитектура, результаты нагрузки, скриншоты
  legacy/               прежняя одностраничная версия
```

## 2. Требования

* Python **3.10 или новее**. На Windows ставьте с python.org и отметьте
  «Add python.exe to PATH».
* Исходящий доступ к публичным API (ключи не нужны):
  `api.binance.com`, `stream.binance.com:9443`, `fapi.binance.com`,
  `fstream.binance.com`, `api.bybit.com`, `stream.bybit.com`.
* Диск: ~8 МБ в сутки на инструмент. По умолчанию 5 инструментов и
  14 дней хранения, это около 0.6 ГБ.
* Память ~100 МБ, нагрузка на CPU — единицы процентов одного ядра.

## 3. Проверка без бирж (1 минута)

```bat
run.bat --demo          (Windows)
./run.sh --demo         (Linux, macOS)
```

Откройте <http://127.0.0.1:8000>. В шапке должен быть статус **DEMO**. Через
~45 секунд появятся 6 часов синтетической истории. Это проверяет, что Python,
зависимости и интерфейс работают. Остановка — Ctrl+C.

При первом запуске скрипт создаёт `backend/.venv` и ставит зависимость
`aiohttp`, нужен доступ к PyPI.

## 4. Запуск с реальными данными

```bat
run.bat                 (Windows)
./run.sh                (Linux, macOS)
```

Что должно произойти:

1. Через 5–30 секунд статус инструментов сменится с CONNECTING на **LIVE**
   (шапка и точки в правой колонке).
2. Свечи Binance за последние 3 дня с разбивкой покупок/продаж появятся сразу.
3. **История стакана начинает копиться с момента запуска.** Прошлый стакан
   биржи не отдают. Полосы дисбаланса и Z-score раскрасятся после прогрева
   базы: по умолчанию сутки, для проверки выберите в панели «База» 1h.

Сервер работает, пока открыто окно. История пишется только пока сервер
запущен, поэтому для реальной работы его держат включённым постоянно
(раздел 8).

## 5. Проверка, что всё работает

Откройте в браузере <http://127.0.0.1:8000/api/health>. Для каждого
инструмента в `instruments` смотрите:

* `status`: `LIVE`;
* `errors`: пусто или единичные старые записи;
* `sync.resyncs` ≥ 1 и не растёт постоянно; `sync.gaps` — редкие;
* `latency_ms.book` и `latency_ms.trades`: десятки–сотни миллисекунд.

Проект проверялся тестами на эмуляторе протоколов. Против живых бирж его ещё
не запускали, поэтому при первом запуске стоит проверить:

* [ ] все 5 инструментов в статусе LIVE;
* [ ] цена совпадает с биржей;
* [ ] через 5–10 минут у Binance BTCUSDT спот видны линии Bid Dominance
      0.25 % и 0.5 %. Линия 1 % может быть пустой: снимок Binance отдаёт
      5000 уровней, а их не всегда хватает до 1 % (это честное «нет
      покрытия», не ошибка);
* [ ] Binance perp: идут сделки (в шапке задержка «сделки … мс»), есть
      funding в панели «Funding»;
* [ ] Bybit: работают полосы 0.25 %; 1 % обычно пустой, потому что Bybit
      отдаёт только верхние 200 (спот) / 500 (фьючерс) уровней.

Если что-то не так — пришлите вывод `/api/health` и окно консоли.

## 6. Настройка

Скопируйте образец и правьте копию:

```bash
cp config/flowlens.config.example.json config/flowlens.config.json
```

`run.bat`, `run.sh` и Docker подхватывают `config/flowlens.config.json`
автоматически. Главное:

```json
{
  "instruments": [
    {"exchange": "binance", "market": "spot", "symbol": "BTCUSDT"},
    {"exchange": "binance", "market": "perp", "symbol": "BTCUSDT"},
    {"exchange": "bybit",   "market": "perp", "symbol": "SOLUSDT"}
  ],
  "retention_days": 14,
  "kline_backfill_days": 3
}
```

* `exchange`: `binance` или `bybit`; `market`: `spot` или `perp`
  (USDT-бессрочные).
* Каждый инструмент — отдельный локальный стакан и WebSocket. 10–20
  инструментов — нормально; сотни — нет.
* Любой параметр можно задать переменной окружения `FLOWLENS_<ИМЯ>`:
  `FLOWLENS_PORT=8080`, `FLOWLENS_RETENTION_DAYS=30`.
* База: `backend/data/flowlens.db` (SQLite). Для резервной копии
  остановите сервер и скопируйте файл.

## 7. Docker

```bash
docker compose up -d --build            # http://127.0.0.1:8000
docker compose logs -f flowlens          # логи
docker compose --profile demo up -d      # DEMO на :8001
```

Данные лежат в томе `flowlens-data`, конфигурация — в `./config`.

## 8. Работа 24/7

**Linux (systemd).** Пример для папки `/opt/flowlens`:

```bash
cd /opt/flowlens/backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
sudo tee /etc/systemd/system/flowlens.service >/dev/null <<'EOF'
[Unit]
Description=FlowLens market data terminal
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/flowlens/backend
Environment=FLOWLENS_CONFIG=/opt/flowlens/config/flowlens.config.json
ExecStart=/opt/flowlens/backend/.venv/bin/python -m flowlens serve
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now flowlens
journalctl -u flowlens -f
```

**Windows.** Планировщик заданий: задача «При входе в систему» или «При
запуске компьютера», действие — `run.bat` из папки проекта. Либо Docker
Desktop с `docker compose up -d`.

## 9. Доступ с других устройств и безопасность

* По умолчанию сервер слушает только `127.0.0.1`, то есть открывается лишь на
  этом компьютере.
* Для локальной сети: `FLOWLENS_HOST=0.0.0.0` и разрешить порт в брандмауэре.
* **В интернет напрямую не выставлять.** У API нет авторизации. Через него
  можно менять алерты, а в базе хранится токен Telegram. Для внешнего доступа
  ставьте обратный прокси с паролем и HTTPS (nginx с basic auth, Caddy) или
  VPN. WebSocket идёт по `/api/stream`, прокси должен его пропускать.

## 10. Частые проблемы

| Симптом | Причина и что делать |
|---|---|
| Баннер «Коллектор пока не может получить данные бирж», в ошибках `451` или `403` | Биржа закрыта для региона или сети. Используйте другую биржу, другую сеть или VPN в рамках местных правил |
| `Cannot connect to host` | Нет интернета, DNS или нужен прокси. Прокси задаётся переменными `HTTPS_PROXY` / `HTTP_PROXY`, сервер их учитывает |
| Binance perp долго в STALE | Binance перевёл WebSocket фьючерсов на маршруты `/public` (стакан) и `/market` (сделки, mark price, ликвидации). Если адреса снова сменились, поправьте `binance_perp_ws_public` и `binance_perp_ws_market` в конфигурации |
| `address already in use` | Порт 8000 занят: `FLOWLENS_PORT=8010` |
| Время на графике «сдвинуто» | Интерфейс показывает местное время браузера, в базе всё в UTC |
| Нет дельты у истории Bybit | Так и есть: в свечах Bybit нет разбивки покупок/продаж, дельта копится с момента запуска |

## 11. Для разработчика

```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest -q                  # 34 теста, ~20 с
python tools/loadtest.py --symbols 10 --seconds 120

cd ../frontend                       # только если правите интерфейс; Node.js 18+
npm install
npm run dev                          # http://localhost:5173, API на :8000
npm run build                        # сборка в backend/flowlens/static
```

Тестовая биржа для экспериментов без сети:
`python -m flowlens mock-exchange --port 9100 --symbols BTCUSDT,ETHUSDT`, затем
в конфигурации `binance_spot_rest: "http://127.0.0.1:9100"` и
`binance_spot_ws: "ws://127.0.0.1:9100"`.
