# Torrent_Parser

Домашний сервер-комбайн: парсер торрент-трекера, который сам ставит раздачи
на закачку, транскодирует полученное видео и отдаёт всё через веб-панель.
Вокруг этого ядра со временем наросли соседние подсистемы — просмотрщик
потоков, рассылка криптовалюты и мобильный кошелёк.

Проект писался под конкретную машину (Ubuntu, MySQL, qBittorrent, systemd),
а не как переносимый продукт: пути, имена сервисов и структура таблиц местами
захардкожены. Читайте его как рабочий инструмент, а не как готовую поставку.

## Из чего состоит

### Ядро: трекер → qBittorrent → транскод → веб

| Файл | Назначение |
|------|-----------|
| [parser.py](parser.py) | Разбор страниц трекера (`requests` + BeautifulSoup), выемка раздач и метаданных |
| [main.py](main.py) | Точка входа обхода: гоняет парсер по разделам и складывает результат в MySQL |
| [db.py](db.py) | Схема и CRUD поверх `mysql-connector-python` |
| [qb.py](qb.py) | Клиент qBittorrent Web API: постановка на закачку, отслеживание готовности |
| [web.py](web.py) | Flask-панель (порт 5000): каталог, карточки, управление очередью транскода, TON-раздел |
| [templates/](templates/) | Шаблоны панели — каталог и профиль |
| [transcode.sh](transcode.sh), [transcode_db.py](transcode_db.py) | Транскодирование ffmpeg с журналированием каждой операции в БД |
| [process_queue.py](process_queue.py) | Последовательный обработчик очереди: pid-файлы, пауза, корректная остановка по SIGTERM |
| [restart.sh](restart.sh) | Перезапуск Flask через systemd (`torrent-parser.service`) с ожиданием порта |

Битрейт для транскода выбирается по разрешению — пороги подобраны замерами
VMAF: до 250 кбит/с выгоднее отдавать 240p, до 600 — 480p, до 2.5 Мбит/с —
720p, выше — 1080p.

### Парсеры и загрузчик медиа

| Файл | Назначение |
|------|-----------|
| [leakgallery_parser.py](leakgallery_parser.py) | Сбор списка профилей из sitemap в таблицу `girls` |
| [leakgallery_media_parser.py](leakgallery_media_parser.py) | Обход профилей, наполнение `girl_media` |
| [media_downloader.py](media_downloader.py) | Скачивание файлов в `data/` с заменой URL в БД на локальные имена |

### `Bonga/` — плеер потоков

Статический HLS-плеер (`player.html` + `hls.min.js`) и обслуживающий его
[Bonga/server.py](Bonga/server.py). Сервер понадобился, потому что
`python -m http.server` умеет только отдавать файлы: база ников жила в
localStorage и была своя у каждого браузера и даже у каждого origin.
Теперь `accounts.json` лежит рядом с плеером и пополняется всеми клиентами
сразу через `GET`/`POST /api/accounts`.

### `ton_payout/` — массовая рассылка TON и USDT

Отдельный модуль со своей [документацией](ton_payout/README.md): читает
получателей из MySQL, рассылает монеты и ведёт журнал запусков, плюс
Flask-панель с логином. Два режима — `highload` (все адреса одной
транзакцией через `WalletHighloadV3R1`, до 64516 штук) и `sequential`
(по одному переводу через `WalletV4R2`, каждый со своим tx-hash).
USDT ходит как джеттон: отдельный jetton-wallet, 6 знаков после запятой,
газ всегда в TON.

### Запоминаемый код TON-адреса

[addr_to_number.py](addr_to_number.py), [grouping.py](grouping.py),
[short_groups.py](short_groups.py) — эксперимент по превращению
TON-адреса в человекочитаемое число: 87 знаков бьются на 22 группы,
из которых для устного подтверждения берутся №1, 8, 15 и 22.

> Код предназначен **только для отображения**. Сверять по нему адрес перед
> отправкой средств нельзя — совпадение четырёх групп не гарантирует
> совпадения адреса целиком.

[gen_ton_addresses.py](gen_ton_addresses.py) детерминированно выводит
адреса из master-сида по `subwallet_id`, равному id записи.

### `otklik/` — прототипы пофрагментной оценки

Три самостоятельных HTML-страницы: вместо одного лайка на весь трек или
ролик зритель оценивает каждый фрагмент отдельно. Подробности в
[otklik/README.md](otklik/README.md).

### `mobile/` — Tonkeeper для Android

Исходники кошелька [Tonkeeper](https://github.com/tonkeeper/android) с
локальными правками сборки. Собственная лицензия лежит в
[mobile/LICENSE](mobile/LICENSE), upstream-история в репозитории не хранится.

### `.claude/` — обвязка Claude Code

Хуки и патчи webview VSCode-расширения: метки времени сообщений, вставка
эмодзи, локализация меню, служебный HTTP-сервер. К самому парсеру отношения
не имеют, но живут в репозитории, потому что настроены под него.
Правила работы описаны в [CLAUDE.md](CLAUDE.md).

## Установка

```bash
git clone https://github.com/bazuevv/Torrent_Parser.git
cd Torrent_Parser

python3 -m venv venv
venv/bin/pip install -r requirements.txt

cp config_secrets.example.py config_secrets.py
$EDITOR config_secrets.py          # доступ к MySQL и qBittorrent
```

Для рассылки TON дополнительно:

```bash
venv/bin/pip install -r ton_payout/requirements.txt
cp ton_payout/config_secrets.example.py ton_payout/config_secrets.py
chmod 600 ton_payout/config_secrets.py     # внутри мнемоника — это доступ к деньгам
$EDITOR ton_payout/config_secrets.py
```

Запуск обхода трекера — `venv/bin/python3 main.py`, веб-панели —
`venv/bin/python3 web.py` (в бою она работает под systemd как
`torrent-parser.service`, тогда перезапуск делается через `./restart.sh`).

## Секреты

Пароли, мнемоники и ключи в репозитории не хранятся. Значения берутся в
таком порядке:

1. переменная окружения с тем же именем (`DB_PASSWORD`, `TON_MNEMONIC`, …);
2. модуль `config_secrets.py` — он в `.gitignore` и в историю не попадал;
3. безопасная заглушка.

Шаблоны с полным перечнем параметров — [config_secrets.example.py](config_secrets.example.py)
и [ton_payout/config_secrets.example.py](ton_payout/config_secrets.example.py).

## Лицензия

Copyright (C) 2026 Владимир (bazuevv)

Программа распространяется на условиях **GNU General Public License версии 3**
или (по вашему выбору) любой более поздней версии. Полный текст — в файле
[LICENSE](LICENSE).

Программа распространяется в надежде, что она будет полезной, но БЕЗ КАКИХ
БЫ ТО НИ БЫЛО ГАРАНТИЙ, включая подразумеваемые гарантии КОММЕРЧЕСКОЙ
ЦЕННОСТИ и ПРИГОДНОСТИ ДЛЯ КОНКРЕТНОЙ ЦЕЛИ. Подробности в тексте лицензии.

Каталог `mobile/` содержит стороннее произведение — Tonkeeper для Android,
распространяемое его авторами на условиях той же GPL-3.0; его собственный
файл лицензии сохранён в [mobile/LICENSE](mobile/LICENSE).
