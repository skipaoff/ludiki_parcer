# Биржи: заметки для адаптеров

У каждого факта стоит пометка, откуда он взят:

- **[прод]** — работало в парсере crypto_pars на сервере (июль 2026);
- **[док]** — сверено с документацией биржи 13.09.2026;
- **[ccxt]** — из исходников ccxt 4.5.78;
- **[проверить]** — ожидаемое поведение, подтвердить на указанном этапе плана;
- **[проверено]** — вызвано терминалом вживую, дата указана.

**Сеть с машины разработки [проверено 13.09.2026].** Публичный REST из дома через перехват HTTPS антивирусом Avast: первый запрос 1–1,3 с (установка TLS), дальше 310–470 мс до обеих бирж. ccxt держит свой набор сертификатов, поэтому терминал включает системное хранилище через `truststore` (`app/system/tls.py`).

---

## Binance USDⓈ-M Futures

**Библиотека:** в ccxt биржа называется `binanceusdm`, есть и REST, и вебсокеты [ccxt].

### Публичный REST (`https://fapi.binance.com`)

| Эндпоинт | Что берём | Статус |
|---|---|---|
| `GET /fapi/v1/ticker/24hr` | `lastPrice`, `quoteVolume` (объём 24ч в USDT), `closeTime` | [прод] |
| `GET /fapi/v1/premiumIndex` | `markPrice` | [прод] |
| `GET /fapi/v1/premiumIndex` | `indexPrice` — цена за единицу котировки: у `1000PEPEUSDT` за 1000 токенов | [проверено 13.09.2026] |
| `GET /fapi/v1/fundingInfo` | интервал фандинга по символу | [прод] |
| `GET /fapi/v1/exchangeInfo` | контракты `contractType=PERPETUAL`, `quoteAsset=USDT`, `status=TRADING` (бывают `PENDING_TRADING`, `SETTLING`, а золото и акции идут как `TRADIFI_PERPETUAL`); фильтры `MARKET_LOT_SIZE` (шаг, минимум и максимум рыночного ордера), `LOT_SIZE`, `MIN_NOTIONAL.notional` (5 USDT), `PRICE_FILTER.tickSize` | [проверено 13.09.2026: 528 торгуемых USDT-перпетуалов] |
| `GET /fapi/v1/ticker/bookTicker` | `bidPrice`, `askPrice` по всем символам — для каталога пар | [проверено 13.09.2026] |
| `GET /fapi/v1/time` | `serverTime` — пинг и расхождение часов раз в 10 с (`probe_clock`) | [проверено 13.09.2026] |

### Вебсокеты [док]

- **Адреса [проверено 13.09.2026]:** `wss://fstream.binance.com/public/stream` для `bookTicker` и стаканов (подписка сообщением `{"method": "SUBSCRIBE", "params": [...], "id": 1}`), `wss://fstream.binance.com/market/stream` для `!markPrice@arr@1s`. Старый `/stream` пока тоже отвечает.
- **Формат кадра:** `{"stream": "...", "data": {...}}`. `bookTicker`: `s`, `b`, `B`, `a`, `A`, `T`, `E`. `depth20@100ms`: `e = depthUpdate`, `b` и `a` — массивы `[цена, количество]` строками, `T`. Ответ на подписку — `{"result": null, "id": 1}`.
- **Частота [проверено]:** `bookTicker` BTCUSDT около 220 сообщений в секунду, 1000PEPEUSDT около 24; `depth20@100ms` около 9 в секунду. Оба потока шлют данные только при изменении: после подписки у тихого контракта `bookTicker` может молчать долго, поэтому терминал раз в 30 с досевает лучшие цены из `GET /fapi/v1/ticker/bookTicker`.
- **`!bookTicker`** — лучшие цены по всем символам, но обновляется **раз в 5 секунд**. Для радара не подходит.
- **`<symbol>@bookTicker`** — лучшие цены по одному символу в реальном времени. Радар строим на них.
- **`<symbol>@depth<5|10|20>@<100ms|250ms|500ms>`** — частичный стакан. Используем `depth20@100ms`.
- **`!markPrice@arr@1s`** — mark и index цены по всем символам [проверить путь `/public` или `/market`, этап 3].
- **Лимит:** до 200 потоков на одно соединение для фьючерсов [ccxt]. Соединение живёт не больше суток, обрывы плановые [проверить, этап 3].

### Проверка ключа и аккаунта (этап 1, `app/exchanges/binance/adapter.py`)

Сырые эндпоинты ccxt, чтобы деньги и ставки приходили строками без float. Форматы по документации [док], подтвердить на первой проверке с настоящим ключом [проверить, этап 1].

| Эндпоинт | Метод ccxt | Что берём |
|---|---|---|
| `GET /sapi/v1/account/apiRestrictions` | `sapi_get_account_apirestrictions` | `enableReading`, `enableFutures`, `enableWithdrawals` (должно быть `false`), `ipRestrict` |
| `GET /fapi/v1/positionSide/dual` | `fapiprivate_get_positionside_dual` | `dualSidePosition`: `true` — hedge, терминал требует `false` |
| `GET /fapi/v3/balance` | `fapiprivatev3_get_balance` | строка `USDT`: `balance`, `availableBalance` |
| `GET /fapi/v1/commissionRate?symbol=BTCUSDT` | `fapiprivate_get_commissionrate` | `takerCommissionRate`, `makerCommissionRate` (доли, не проценты) |

Подпись: ccxt подписывает запрос локальным временем минус `options["timeDifference"]`; терминал выставляет его из каждого замера часов.

Демо-торговля: `enable_demo_trading(True)` переключает fapi на `demo-fapi.binance.com` [ccxt]. У демо нет `sapi`, поэтому права ключа не запрашиваются; демо-ключи хранятся отдельно (`binance-demo:*`).

### Приватная часть (через ccxt) [проверить, этапы 5, 6]

- Позиции, цена ликвидации.
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
| то же | **лучшие цены — `bid1` и `ask1`**, индекс — `indexPrice` | [проверено 13.09.2026] |
| то же | `maxBidPrice` и `minAskPrice` — это **ценовые лимиты** ордеров (у BTC 84 876 и 69 444 при цене 77 123), а не лучшие цены стакана. Первая версия плана ошибочно брала их для радара | [проверено 13.09.2026] |
| `GET https://contract.mexc.com/api/v1/contract/funding_rate` | ставки фандинга | [прод] |
| `GET /api/v1/contract/detail` | `contractSize` (базовых единиц в контракте: у `PEPE_USDT` 10 000 000 PEPE, у `1000BONK_USDT` 10 000 единиц «1000BONK»), `volUnit` (шаг в контрактах), `minVol`, `maxVol`, `priceUnit`; фильтр: `futureType=1`, `quoteCoin=settleCoin=USDT`, `state=0`, `apiAllowed=true` (44 USDT-контракта из 1069 через API не торгуются), `isHidden=false` | [проверено 13.09.2026: 1025 контрактов] |
| то же | `takerFeeRate` и `makerFeeRate` — публичные ставки по контракту; бывают нулевыми (зона `mc-trade-zone-0fees`). Для API объявлены 0,05% тейкер, поэтому до проверки ключей лента считает по 0,05% | [проверено 13.09.2026] |

В журнале изменений от 19.01.2026 базовый домен фьючерсного API сменился на `https://api.mexc.com` [док]. Парсер в июле 2026 ещё работал через `contract.mexc.com`. **Решено на этапе 1:** терминал ходит через `https://api.mexc.com/api/v1/contract` и `/api/v1/private` — так настроен ccxt 4.5.78 [ccxt]; `GET /api/v1/contract/ping` отвечает `{"success": true, "code": 0, "data": <время сервера>}` [проверено 13.09.2026].

### Проверка ключа и аккаунта (этап 1, `app/exchanges/mexc/adapter.py`)

Ответы обёрнуты в `{"success", "code", "data"}`. Форматы по документации [док], подтвердить на первой проверке с настоящим ключом [проверить, этап 1].

| Эндпоинт | Метод ccxt | Что берём |
|---|---|---|
| `GET /api/v1/private/position/position_mode` | `contract_private_get_position_position_mode` | `data`: 1 — hedge, 2 — one-way |
| `GET /api/v1/private/account/assets` | `contract_private_get_account_assets` | строка `USDT`: `equity` (или `cashBalance`), `availableBalance` — числа JSON |
| `GET /api/v1/private/account/contract/fee_rate` | `contract_private_get_account_contract_fee_rate` | ставки тейкера и мейкера; имена полей не подтверждены, адаптер пробует `takerFeeRate`/`takerFee` |
| `GET /api/v1/contract/detail?symbol=BTC_USDT` | `contract_public_get_detail` | запасной источник ставок по умолчанию: `takerFeeRate`, `makerFeeRate` |

Права ключа для фьючерсов MEXC не отдаёт, поэтому в проверке они «неизвестно» и висит предупреждение: вывод у ключа выключается вручную при выпуске.

### Вебсокеты

- **Адрес:** `wss://contract.mexc.com/edge` [ccxt].
- **`{"method": "sub.tickers", "param": {}}`** — все символы (1262 записи) раз в 2 секунды. **Лучших цен в нём нет [проверено 13.09.2026]:** только `lastPrice`, `fairPrice`, `indexPrice`, `amount24`, `volume24` и ценовые лимиты `maxBidPrice`/`minAskPrice`. Поэтому радар MEXC строится на опросе `GET /api/v1/contract/ticker` раз в секунду: там есть `bid1`, `ask1` и `timestamp`.
- **`sub.ticker`** по одному символу: `bid1` и `ask1` есть, но пуш раз в 2–3 с [проверено].
- **`{"method": "sub.depth.full", "param": {"symbol": "PEPE_USDT", "limit": 20}}`** — полный снимок 20 уровней, около 2 раз в секунду у ликвидных контрактов и только при изменениях у тихих [проверено]. Терминал использует его: снимок не требует сборки по версиям. Отписка — `unsub.depth.full`.
- **Уровень стакана — `[цена, объём в контрактах, число ордеров]` [проверено 13.09.2026].** Первая версия плана считала порядок `[цена, ордера, объём]` — объём стакана считался бы неверно. В токены переводим через `contractSize`.
- **`sub.depth`** — изменения стакана с `version`, `begin`, `end`; при пропуске номера стакан пересобирается. Терминал его пока не использует.
- **Ответы:** подписка — `{"channel": "rs.sub.depth.full", "data": "success"}`; ошибки — канал `rs.error`. Пинг `{"method": "ping"}` терминал шлёт раз в 15 с, иначе MEXC закрывает соединение через минуту.
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
