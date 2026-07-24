# TON Payout — рассылка TON по списку из БД + веб-панель

Модуль автоматической рассылки криптовалюты TON на список адресов, с
веб-интерфейсом управления и запуском по расписанию (cron).

## Из чего состоит

| Файл | Назначение |
|------|-----------|
| `db.py` | Таблицы `ton_recipients`, `ton_payout_runs`, `ton_payout_run_items` в MySQL + CRUD |
| `payout_core.py` | Логика отправки: читает активных получателей из БД, шлёт, пишет журнал |
| `run.py` | CLI-раннер (`python3 -m ton_payout.run`) для cron и для веб-процесса |
| `web.py` | Flask-панель с логином/паролем: получатели, запуск, история |
| `config.py` / `config_secrets.py` | Настройки и секреты (мнемоника, доступ к БД, пароль веба) |

## Два режима рассылки

- **`highload`** (по умолчанию) — все получатели одной транзакцией через
  `WalletHighloadV3R1` (до 64516 адресов). Быстро и дёшево по комиссии.
  Подтверждение — на уровне всей транзакции.
- **`sequential`** — по одному переводу через `WalletV4R2` + `SeqnoGuard`,
  каждый подтверждается отдельно (свой tx-hash). Надёжно, но медленно;
  для небольшого числа адресов.

> ⚠️ Это **разные контракты с разными адресами** даже из одной мнемоники.
> Пополнять нужно тот адрес, которым будете слать (адреса видны в панели в
> блоке «Кошелёк-отправитель» и в dry-run логе).

## Установка

```bash
cd /path/to/Torrent_Parser
venv/bin/pip install -r ton_payout/requirements.txt

cp ton_payout/config_secrets.example.py ton_payout/config_secrets.py
chmod 600 ton_payout/config_secrets.py     # там мнемоника — доступ к деньгам
$EDITOR ton_payout/config_secrets.py       # впишите мнемонику, пароль веба, SECRET_KEY
```

Сгенерировать `SECRET_KEY`:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Инициализация таблиц (создаются автоматически при первом запуске веба/раннера,
но можно и вручную):
```bash
venv/bin/python3 -c "from ton_payout import db; db.init_db()"
```

## Веб-панель

Разработка:
```bash
venv/bin/python3 -m ton_payout.web      # http://127.0.0.1:8091
```

Продакшн (gunicorn, слушает только localhost — наружу отдавать через
reverse-proxy с HTTPS или SSH-туннель):
```bash
venv/bin/gunicorn -b 127.0.0.1:8091 ton_payout.web:app
```

Вход — по `WEB_USERNAME` / `WEB_PASSWORD` из `config_secrets.py`.

## Встроенная вкладка «Переводы» (основное приложение на :5000)

Помимо отдельной панели, управление рассылкой встроено в основное приложение
Torrent_Parser как вкладка **«Переводы»** (`web.py`, эндпоинты `/api/ton/*`).

Модель доступа к вкладке:
1. быть admin-eligible — заходить с **localhost или Разрешённого IP** (та же
   проверка, что и у остального админ-функционала приложения);
2. **плюс** войти по логину/паролю (`WEB_USERNAME` / `WEB_PASSWORD`) — пароль
   спрашивается **всегда**, в том числе с localhost.

Изменяющие действия (добавить получателя, запустить рассылку) дополнительно
защищены CSRF-токеном сессии. Запуск рассылки уходит в тот же процесс
`ton_payout.run`, что и cron.

Чтобы включить вкладку:
```bash
cd /path/to/Torrent_Parser
venv/bin/pip install -r ton_payout/requirements.txt   # ставит tonutils в venv приложения
cp ton_payout/config_secrets.example.py ton_payout/config_secrets.py
chmod 600 ton_payout/config_secrets.py
$EDITOR ton_payout/config_secrets.py   # WEB_USERNAME, WEB_PASSWORD, SECRET_KEY, TON_MNEMONIC
./restart.sh                           # перезапуск Flask (иначе правки не подхватятся)
```
Секреты можно держать и в корневом `config_secrets.py` — загрузчик
`ton_payout/config.py` читает оба файла (свой имеет приоритет).

Если `WEB_PASSWORD` не задан, вкладка показывает сообщение и вход невозможен.
Если `tonutils` не установлен в venv, управление получателями и историей всё
равно работает, но баланс кошелька и запуск рассылки — нет.

## Запуск по расписанию (раз в месяц)

Cron, 3:00 первого числа каждого месяца (секреты берутся из
`config_secrets.py`, в crontab их писать не нужно):
```cron
0 3 1 * * cd /path/to/Torrent_Parser && venv/bin/python3 -m ton_payout.run --mode highload >> ton_payout/logs/cron.log 2>&1
```

Проверить без отправки денег:
```bash
venv/bin/python3 -m ton_payout.run --mode highload --dry-run
```

Коды выхода раннера: `0` — всё отправлено (или dry-run прошёл),
`1` — ошибка, `2` — часть переводов не прошла (только режим sequential).

## Безопасность

- **Мнемоника = деньги.** Держите `config_secrets.py` с правами `600`,
  в git он не попадает (`.gitignore`). Ещё безопаснее — задавать `TON_MNEMONIC`
  и пароли как переменные окружения (в systemd `EnvironmentFile=`), а файл
  не хранить на диске вовсе.
- **Веб-панель управляет реальными деньгами.** Не выставляйте порт наружу без
  HTTPS и, желательно, без дополнительного сетевого ограничения (VPN/SSH-туннель).
  Логин/пароль — минимальная, но не единственная линия защиты.
- **Сначала testnet.** Перед первым боевым запуском поставьте
  `TON_NETWORK = "testnet"`, получите тестовые TON и прогоните полный цикл.
- **Dry-run** встроен в панель (галочка «Пробный прогон») и в CLI (`--dry-run`):
  проверяет баланс и пишет план, но ничего не отправляет.
- Защита от двойного старта: пока боевая рассылка в статусе `running`, панель
  не даст запустить вторую.
