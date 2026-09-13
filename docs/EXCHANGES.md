# Биржи: заметки для адаптеров

У каждого факта стоит пометка, откуда он взят:

- **[прод]** — работало в парсере crypto_pars на сервере (июль 2026);
- **[док]** — сверено с документацией биржи 13.09.2026;
- **[ccxt]** — из исходников ccxt 4.5.78;
- **[проверить]** — ожидаемое поведение, подтвердить на указанном этапе плана.

---

## Binance USDⓈ-M Futures

**Библиотека:** в ccxt биржа называется `binanceusdm`, есть и REST, и вебсокеты [ccxt].

### Публичный REST (`https://fapi.binance.com`)

| Эндпоинт | Что берём | Статус |
|---|---|---|
| `GET /fapi/v1/ticker/24hr` | `lastPrice`, `quoteVolume` (объём 24ч в USDT), `closeTime` | [прод] |
| `GET /fapi/v1/premiumIndex` | `markPrice` | [прод] |
| `GET /fapi/v1/premiumIndex` | `indexPrice` | [проверить, этап 2] |
| `GET /fapi/v1/fundingInfo` | интервал фандинга по символу | [прод] |
| `GET /fapi/v1/exchangeInfo` | фильтры `LOT_SIZE`, `MARKET_LOT_SIZE` (отдельный лимит для рыночных ордеров), `MIN_NOTIONAL`, `PRICE_FILTER` | [проверить, этап 2] |
| `GET /fapi/v1/time` | серверное время для расчёта расхождения часов | [проверить, этап 1] |

### Вебсокеты [док]

- **Адреса:** `wss://fstream.binance.com/public/ws/<stream>` и `wss://fstream.binance.com/public/stream?streams=<a>/<b>`.
- **`!bookTicker`** — лучшие цены по всем символам, но обновляется **раз в 5 секунд**. Для радара не подходит.
- **`<symbol>@bookTicker`** — лучшие цены по одному символу в реальном времени. Радар строим на них.
- **`<symbol>@depth<5|10|20>@<100ms|250ms|500ms>`** — частичный стакан. Используем `depth20@100ms`.
- **`!markPrice@arr@1s`** — mark и index цены по всем символам [проверить путь `/public` или `/market`, этап 3].
- **Лимит:** до 200 потоков на одно соединение для фьючерсов [ccxt]. Соединение живёт не больше суток, обрывы плановые [проверить, этап 3].

### Приватная часть (через ccxt) [проверить, этапы 1, 5, 6]

- Баланс, позиции, цена ликвидации.
- Права ключа: `GET /sapi/v1/account/apiRestrictions`, поле `enableWithdrawals` должно быть `false`.
- Режим позиций: `dualSidePosition`. В MVP требуем one-way.
- Плечо и режим маржи по символу.
- Комиссия аккаунта по символу.
- Рыночный ордер с `reduceOnly` и `newClientOrderId`. У ccxt есть `create_order_ws` для отправки ордера через торговый вебсокет [ccxt].
- User data stream через `listenKey`: ордера, позиции, баланс.
- История начислений фандинга.

### Тестирование

Сначала прогон на демо-торговле Binance [проверить поддержку в ccxt, этап 6].

---

## MEXC Futures

**Библиотека:** в ccxt биржа называется `mexc`, рынок `swap`. Есть REST и вебсокеты [ccxt].

**Торговля через API** открыта с 31.03.2026 для всех пользователей с пройденной KYC. Нужен ключ с правом на фьючерсы. Комиссии API: мейкер 0,01%, тейкер 0,05% [док].

### Публичный REST

| Эндпоинт | Что берём | Статус |
|---|---|---|
| `GET https://contract.mexc.com/api/v1/contract/ticker` | `lastPrice`, `fairPrice`, `amount24` (объём 24ч в USDT) | [прод] |
| то же | `bid1`, `ask1`, `indexPrice` | [проверить, этап 2] |
| `GET https://contract.mexc.com/api/v1/contract/funding_rate` | ставки фандинга | [прод] |
| `GET /api/v1/contract/detail` | `contractSize`, `minVol`, `maxVol`, шаг объёма и цены | [проверить, этап 2] |

В журнале изменений от 19.01.2026 базовый домен фьючерсного API сменился на `https://api.mexc.com` [док]. Парсер в июле 2026 ещё работал через `contract.mexc.com`. Какой домен использовать, решаем на этапе 1.

### Вебсокеты

- **Адрес:** `wss://contract.mexc.com/edge` [ccxt].
- **`{"method": "sub.tickers", "param": {}}`** — все символы раз в 2 секунды [док]. Поля:
  - `maxBidPrice` (лучший bid) и `minAskPrice` (лучший ask);
  - `fairPrice`, `indexPrice`, `amount24`, `lastPrice`.
- **`{"method": "sub.depth", "param": {"symbol": "BTC_USDT"}}`** — стакан по символу раз в 200 мс [док]:
  - уровень приходит как `[price, orders_count, qty]`, где `qty` в **контрактах**; в токены переводим через `contractSize`;
  - в сообщении есть `version`. Если номер версии пропущен, стакан пересобирается.
- **Приватные каналы** после `login` [ccxt]: `push.personal.order`, `push.personal.asset`, `push.personal.order.deal`. Позиции `push.personal.position` [проверить, этап 5].
- **Лимит подписок:** у спота 200 на соединение с 2023 года [док]. Для фьючерсов [проверить, этап 3].
- **Пинги** держит библиотека [проверить, этап 3].

### Приватная часть (через ccxt) [проверить, этапы 1, 5, 6]

- Баланс, позиции, цена ликвидации.
- Режим позиций (hedge или one-way), плечо и тип маржи (изолированная или кросс).
- Рыночный ордер: объём `vol` в **контрактах**, признак reduce-only, свой id ордера.
- Права ключа через API, если биржа их отдаёт. Если нет — предупреждение в настройках.
- История фандинга.
- Торговые эндпоинты бывают на обслуживании. Такие ошибки блокируют кнопку по бирже.

### Тестирование

Есть ли у MEXC API-демо для фьючерсов, неизвестно [проверить, этап 6]. Если нет, тестируем на минимальных живых суммах.

---

## Остальные биржи (после MVP)

Публичные эндпоинты тикеров, которые работали в парсере [прод]. Для терминала у каждой биржи дополнительно нужны bid/ask, стакан и приватная часть.

| Биржа | id в ccxt | Тикеры (все символы) | Цена в парсере | Объём 24ч в парсере | Заметки |
|---|---|---|---|---|---|
| Bybit | `bybit` | `GET https://api.bybit.com/v5/market/tickers?category=linear` | `lastPrice` | `turnover24h` | |
| OKX | `okx` | `GET https://www.okx.com/api/v5/market/tickers?instType=SWAP` | `last` | `volCcy24h` | у SWAP `volCcy24h`, похоже, в базовой монете, а не в $ — парсер считал его долларами [проверить]. Торгуется контрактами |
| Gate | `gate` | `GET https://api.gateio.ws/api/v4/futures/usdt/tickers` | `last` | `volume_24h_settle` | торгуется контрактами |
| Bitget | `bitget` | `GET https://api.bitget.com/api/v2/mix/market/tickers?productType=USDT-FUTURES` | `lastPr` | `quoteVolume` | |
| BingX | `bingx` | `GET https://open-api.bingx.com/openApi/swap/v2/quote/ticker` | `lastPrice` | `quoteVolume` | |
| Phemex | `phemex` | `GET https://api.phemex.com/md/v3/ticker/24hr/all?type=Perpetual` | `lastRp` | `turnoverRp` (в парсере делится на 1e8) | масштаб поля [проверить] |
| Hyperliquid | `hyperliquid` | `POST https://api.hyperliquid.xyz/info` `{"type":"metaAndAssetCtxs"}` | `midPx` | `dayNtlVlm` | префикс `k` = ×1000. Торговля через API-кошелёк без права вывода, а не API-ключ |
| Aster | `aster` | `GET https://fapi.asterdex.com/fapi/v1/ticker/24hr` | `lastPrice` | `quoteVolume` | формат API как у Binance |
| KuCoin Futures | `kucoinfutures` | (в парсере только фандинг) `GET https://api-futures.kucoin.com/api/v1/contracts/active` | — | — | `XBT` = BTC, суффикс `USDTM` |
| Variational | нет | `GET https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats` | `mark_price` | — | торгового API в ccxt нет. Для терминала не подходит |
