# Терминал на сервере

Зачем: терминал смотрит рынок круглосуточно, шлёт вилки в Telegram и копит историю, пока ноутбук закрыт.

**Режим по умолчанию — наблюдатель: ключей бирж на сервере нет.** Терминал читает только публичные данные. Если сервер взломают, терять нечего. Торговля остаётся на твоей машине.

Торговый режим возможен позже и обсуждается отдельно: у сервера постоянный IP, а Binance торговый ключ без привязки к IP вообще не выдаёт (`docs/KEYS.md`). Но ключи на сервере — это другой уровень риска, и включать их заодно с переездом не стоит.

---

## Что нужно на машине

Ubuntu 24.04 или новее, 2 ядра, 4 ГБ памяти, 40 ГБ диска. Терминал считает 19 тысяч пар за такт около 30 мс на ноутбуке; серверу этого хватает с запасом. База растёт примерно на единицы гигабайт в квартал — сроки хранения уже настроены (`db/002_timescale.sql`).

## Установка

Всё делается один раз, под пользователем с `sudo`.

**1. Пользователь и каталоги.**

```bash
sudo adduser --system --group --home /opt/ludik ludik
sudo mkdir -p /opt/ludik/ludik-data /etc/ludik
sudo chown -R ludik:ludik /opt/ludik
```

**2. PostgreSQL 18 с TimescaleDB.**

```bash
sudo apt install -y postgresql-18 postgresql-client-18
```

```bash
sudo sh -c "echo 'deb https://packagecloud.io/timescale/timescaledb/ubuntu/ $(lsb_release -cs) main' > /etc/apt/sources.list.d/timescaledb.list" \
  && wget --quiet -O - https://packagecloud.io/timescale/timescaledb/gpgkey | sudo gpg --dearmor -o /etc/apt/trusted.gpg.d/timescaledb.gpg \
  && sudo apt update && sudo apt install -y timescaledb-2-postgresql-18
```

```bash
sudo timescaledb-tune --quiet --yes && sudo systemctl restart postgresql
```

**3. База и её пароль.**

```bash
sudo -u postgres psql -c "CREATE USER ludik WITH PASSWORD 'сюда-длинный-пароль'" -c "CREATE DATABASE ludik OWNER ludik"
```

**4. Код и зависимости.**

```bash
sudo -u ludik git clone https://github.com/skipaoff/ludiki_parcer.git /opt/ludik/ludiki_parcer
```

```bash
curl -LsSf https://astral.sh/uv/install.sh | sudo -u ludik sh
```

```bash
cd /opt/ludik/ludiki_parcer && sudo -u ludik /opt/ludik/.local/bin/uv sync --frozen
```

Интерфейс собирается один раз — он нужен, даже если смотреть его будешь редко:

```bash
sudo apt install -y nodejs npm && cd /opt/ludik/ludiki_parcer/web && sudo -u ludik npm ci && sudo -u ludik npm run build
```

**5. Настройки.**

```bash
sudo -u ludik cp /opt/ludik/ludiki_parcer/deploy/config.server.toml /opt/ludik/ludiki_parcer/config.toml
```

В нём заполняется `chats` для Telegram — список получателей; остальное уже под сервер: интерфейс без браузера, кластером управляет systemd, торговля выключена.

**6. Секреты.** На сервере нет связки ключей, поэтому секреты приходят из окружения. Файл читает только служба:

```bash
sudo install -m 600 -o ludik -g ludik /dev/null /etc/ludik/ludik.env
```

```bash
sudo tee /etc/ludik/ludik.env > /dev/null <<'EOF'
LUDIK_POSTGRES_LUDIK=сюда-тот-же-пароль-от-базы
LUDIK_TELEGRAM_BOT_TOKEN=токен-бота-от-BotFather
EOF
```

Имена переменных строятся из имён секретов: `postgres:ludik` → `LUDIK_POSTGRES_LUDIK`, `telegram:bot_token` → `LUDIK_TELEGRAM_BOT_TOKEN`. Записывать секреты терминал на сервере не умеет — только читать: это осознанно.

**7. Служба.**

```bash
sudo cp /opt/ludik/ludiki_parcer/deploy/ludik.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now ludik
```

## Проверка

```bash
systemctl status ludik --no-pager
```

```bash
journalctl -u ludik -f
```

В первые полминуты в журнале должны появиться `app.started`, миграции базы и `link_up` по каждой бирже. Через минуту-другую — первые вилки. Telegram молчит первые 20 секунд после запуска намеренно: то, что уже висит на экране, не новость.

**Интерфейс** слушает только петлю. С ноутбука:

```bash
ssh -L 8765:127.0.0.1:8765 пользователь@сервер
```

Дальше открыть `http://127.0.0.1:8765` — токен сессии печатается в журнале при старте (`journalctl -u ludik | grep '#t='`).

## Телеграм-бот

1. У [@BotFather](https://t.me/BotFather) — `/newbot`, забрать токен в `/etc/ludik/ludik.env`.
2. Узнать id чата: написать боту что-нибудь и открыть `https://api.telegram.org/bot<токен>/getUpdates` — в ответе будет `chat.id`. Для канала бота нужно сделать администратором, id канала начинается с `-100`.
3. Вписать получателей в `chats` в `config.toml` и перезапустить: `sudo systemctl restart ludik`. Каждый адресат должен сам написать боту хотя бы раз, иначе Telegram откажет: бот не пишет первым.

Пока токена нет, терминал не молчит: при `enabled = true` и пустом токене сообщения пишутся в журнал целиком — так можно посмотреть, что и как он шлёт, ничего не подключая.

## Обновление

```bash
cd /opt/ludik/ludiki_parcer && sudo -u ludik git pull && sudo -u ludik /opt/ludik/.local/bin/uv sync --frozen && (cd web && sudo -u ludik npm ci && sudo -u ludik npm run build) && sudo systemctl restart ludik
```

Миграции базы применяются при старте сами; применённый файл терминал больше не трогает и сверяет контрольные суммы.

## Резервная копия

История — единственное, что невосполнимо: рынок вчерашнего дня повторно не соберёшь.

```bash
sudo -u postgres pg_dump -Fc ludik > /opt/ludik/ludik-data/backup-$(date +%F).dump
```

Ставится в `cron` раз в сутки; хранить лучше не на этом же сервере.

## Что важно помнить

- **Сервер шлёт, но не торгует.** Уведомление приходит на телефон, а кнопка «Открыть» живёт в терминале на твоей машине. Вилки живут минутами — если хочешь брать их с телефона, это тот самый торговый режим, который включается отдельно.
- **Часы.** Серверное время должно быть синхронизировано (`timedatectl`): расхождение с биржами терминал меряет сам и предупреждает, но подписи запросов в торговом режиме этого не прощают.
- **Диск.** Следить за `/opt/ludik/ludik-data`: буфер записи растёт, если база недоступна.
- **Порт наружу не открывать.** Интерфейс требует токен и слушает петлю; выставлять его в интернет незачем — есть SSH.
