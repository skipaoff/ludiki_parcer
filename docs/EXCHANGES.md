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
- **`!markPrice@arr@1s`** — mark (`p`) и index (`i`) цены по всем символам раз в секунду, маршрут `/market` [проверено 13.09.2026]. Терминал пишет их в снимки радара.
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

### Позиции, баланс, фандинг (этап 5) [док, проверить с ключами]

| Эндпоинт | Метод ccxt | Что берём |
|---|---|---|
| `GET /fapi/v2/positionRisk` | `fapiprivatev2_get_positionrisk` | `positionAmt` (знак — сторона, в единицах контракта), `entryPrice`, `markPrice`, `liquidationPrice` (за единицу котировки), `leverage`, `marginType`, `updateTime`. v2 выбран, потому что v3 не отдаёт плечо и тип маржи |
| `GET /fapi/v3/account` | `fapiprivatev3_get_account` | `totalMarginBalance`, `availableBalance`, `totalInitialMargin` |
| `GET /fapi/v1/income?incomeType=FUNDING_FEE` | `fapiprivate_get_income` | сумма `income` с момента открытия пары |

### Ордера (этап 6) [док, проверить пробной сделкой]

| Эндпоинт | Метод ccxt | Что делаем |
|---|---|---|
| `POST /fapi/v1/leverage` | `fapiprivate_post_leverage` | плечо по символу |
| `POST /fapi/v1/marginType` | `fapiprivate_post_margintype` | `ISOLATED`/`CROSSED`; ответ `-4046` («не нужно менять») — не ошибка |
| `POST /fapi/v1/order` | `fapiprivate_post_order` | `type=MARKET`, `quantity` в единицах контракта, `newClientOrderId`, `reduceOnly=true` на закрытие, `newOrderRespType=RESULT` — в ответе сразу `status`, `executedQty`, `avgPrice` |
| `GET /fapi/v1/order?origClientOrderId=` | `fapiprivate_get_order` | статус по своему id; `-2013` — ордера нет |
| `GET /fapi/v1/userTrades?orderId=` | `fapiprivate_get_usertrades` | исполнения: `price`, `qty`, `commission` (может быть отрицательной), `commissionAsset` |
| `POST /fapi/v1/listenKey`, `PUT` раз в 30 минут | `fapiprivate_post_listenkey`, `fapiprivate_put_listenkey` | user data stream `wss://fstream.binance.com/ws/<listenKey>`: `ORDER_TRADE_UPDATE` (`o.c` — свой id), `ACCOUNT_UPDATE`, `listenKeyExpired` |

Ошибки: таймаут и `-1007` («execution status unknown») — исход неизвестен, ордер ищется по своему id; отказ с кодом — ордер не принят.

### Приватная часть

- Цена ликвидации в потоке `ACCOUNT_UPDATE` не приходит — берётся из REST.
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

### Позиции, баланс, фандинг (этап 5) [док, проверить с ключами]

| Эндпоинт | Метод ccxt | Что берём |
|---|---|---|
| `GET /api/v1/private/position/open_positions` | `contract_private_get_position_open_positions` | `holdVol` (контракты), `positionType` 1 лонг / 2 шорт, `holdAvgPrice`, `liquidatePrice`, `leverage`, `openType` 1 изолированная / 2 кросс. Mark цены в ответе нет — берётся `fairPrice` из опроса тикеров |
| `GET /api/v1/private/account/assets` | `contract_private_get_account_assets` | USDT: `equity`, `availableBalance`, `positionMargin` + `frozenBalance` |
| `GET /api/v1/private/position/funding_records` | `contract_private_get_position_funding_records` | `resultList[].funding` с `settleTime` не раньше открытия пары, постранично до 20 страниц |

### Ордера (этап 6) [док, проверить пробной сделкой]

| Эндпоинт | Метод ccxt | Что делаем |
|---|---|---|
| `POST /api/v1/private/position/change_leverage` | `contract_private_post_position_change_leverage` | без позиции: `symbol`, `leverage`, `openType` 1/2, `positionType` 1 и 2 |
| `POST /api/v1/private/order/submit` | `contract_private_post_order_submit` | `vol` в контрактах, `side` 1 открыть лонг / 2 закрыть шорт / 3 открыть шорт / 4 закрыть лонг, `type=5` (рыночный), `openType`, `leverage`, `externalOid`; ответ несёт только id ордера |
| `GET /api/v1/private/order/external/{symbol}/{external_oid}` | `contract_private_get_order_external_symbol_external_oid` | `state` 1 новый / 2 исполняется / 3 исполнен / 4 отменён / 5 недействителен, `dealVol`, `dealAvgPrice`, `takerFee`, `makerFee`. Сразу после отправки статус может ещё не найтись — адаптер опрашивает до 4 раз через 150 мс |
| `GET /api/v1/private/order/deal_details/{order_id}` | `contract_private_get_order_deal_details_order_id` | исполнения: `vol`, `price`, `fee`, `feeCurrency`, `isTaker` |
| `wss://contract.mexc.com/edge`, `{"method": "login", "param": {"apiKey", "reqTime", "signature"}}` | — | подпись HMAC-SHA256 от `apiKey + reqTime`; после входа приходят `push.personal.order` (`externalOid`), `push.personal.position`, `push.personal.asset` |

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

## Gate USDT Futures (подключена 14.09.2026)

**Библиотека:** в ccxt биржа называется `gate`, рынок `swap`, методы `public_futures_*` и `private_futures_*` с параметром `settle=usdt`.

### Публичная часть [проверено 14.09.2026]

| Эндпоинт | Что берём |
|---|---|
| `GET /api/v4/futures/usdt/contracts` | 981 контракт. `name` (`BTC_USDT`), `quanto_multiplier` — **токенов в одном контракте** (BTC 0,0001, PEPE 10 000 000), `order_size_min`/`order_size_max`/`market_order_size_max` в контрактах, `order_price_round`, `status` (`trading`), `in_delisting`, `is_pre_market`, `type` (`direct`), `enable_decimal`, `taker_fee_rate` (0,00075). Контракты с дробным размером терминал торгует целыми контрактами |
| `GET /api/v4/futures/usdt/tickers` | `highest_bid`, `lowest_ask`, `mark_price`, `index_price`, `volume_24h_quote` (оборот в USDT). Терминал опрашивает раз в секунду — это радар Gate |
| `GET /api/v4/spot/time` | `server_time` в мс — пинг и часы |
| `wss://fx-ws.gateio.ws/v4/ws/usdt`, `futures.order_book` с `["BTC_USDT", "20", "0"]` | событие `all` — **полный снимок** 20 уровней около 9 раз в секунду; уровень `{"p": цена, "s": размер в контрактах}`. Подписка: `{"time", "channel", "event": "subscribe", "payload"}`, пинг `futures.ping` |
| то же, `futures.book_ticker` | `b`, `B`, `a`, `A`, `t` в реальном времени (терминал пока не использует) |

Цены на Gate — за токен, множителей в тикерах не встретилось. У некоторых тикеров Gate другой актив, чем у одноимённых на Binance и MEXC (CAT, EDGE, HK50): терминал видит расхождение цены за токен больше 20% и убирает такие пары из радара, пока их не отметят «проверено».

### Приватная часть [док, проверить пробной сделкой]

| Эндпоинт | Что делаем |
|---|---|
| `GET /futures/usdt/accounts` | `total`, `unrealised_pnl`, `available`, `position_margin`, `order_margin`, `in_dual_mode` (one-way = `false`) |
| `GET /futures/usdt/fee?contract=` | `taker_fee`, `maker_fee` долями |
| `GET /api/v4/account/detail` | `ip_whitelist`. Права ключа на вывод Gate не сообщает — предупреждение |
| `GET /futures/usdt/positions` | `size` в контрактах со знаком, `entry_price`, `mark_price`, `liq_price`, `leverage` (0 = кросс) |
| `POST /futures/usdt/positions/{contract}/leverage` | `leverage` для изолированной, `leverage=0` + `cross_leverage_limit` для кросса |
| `POST /futures/usdt/orders` | `size` со знаком (плюс покупка), `price="0"` + `tif="ioc"` — рыночный, `text="t-<свой id>"`, `reduce_only` |
| `GET /futures/usdt/orders/{t-свой id}` | **по своему id ордер находится только 60 секунд после исполнения**; позже адаптер ищет его среди последних 100 ордеров контракта, и только потом считает, что ордера не было |
| `GET /futures/usdt/my_trades?order=` | исполнения, `fee` в USDT |
| `GET /futures/usdt/account_book?type=fund` | фандинг, `change` со знаком |

Приватный вебсокет Gate терминал пока не использует: подтверждение идёт запросом статуса и опросом позиций.

---

## Aster Futures (подключена 14.09.2026)

**Библиотека:** в ccxt биржа называется `aster`. API повторяет Binance USDⓈ-M: те же поля, фильтры, коды ошибок — адаптер использует парсеры Binance.

### Публичная часть [проверено 14.09.2026]

| Эндпоинт | Что берём |
|---|---|
| `GET https://fapi.asterdex.com/fapi/v1/exchangeInfo` | 594 символа, из них торгуются 562 USDT-бессрочных (`status` `TRADING`, `contractType` `PERPETUAL`, `quoteAsset`/`marginAsset` `USDT`). Остальные — `SETTLING`, `PENDING_TRADING`, котировки `USD1` и `U`. Фильтры как у Binance: `MARKET_LOT_SIZE` (BTC: шаг 0,001, максимум 120), `MIN_NOTIONAL` 5, `PRICE_FILTER`. `1000PEPEUSDT` — цена и количество за 1000 PEPE |
| `GET /fapi/v1/ticker/bookTicker` (все символы) | `bidPrice`, `askPrice`, `time` — подсев лучших цен тихих контрактов раз в 30 с |
| `GET /fapi/v1/premiumIndex` | `markPrice`, `indexPrice`; в ответе есть и не-USDT символы (`GNSUSD`) |
| `GET /fapi/v1/ticker/24hr` | `quoteVolume` |
| `GET /fapi/v1/time` | `serverTime` — пинг и часы |
| `wss://fstream.asterdex.com/stream`, `{"method":"SUBSCRIBE","params":[...]}` | один адрес для всех потоков, до 200 потоков на соединение, 10 управляющих сообщений в секунду, соединение живёт 24 часа. **`!bookTicker` (все символы) — в реальном времени**: за 15 с на BTC, ETH, SOL, DOGE, ASTER пришло ровно столько же обновлений, сколько в отдельных `<symbol>@bookTicker` (у Binance этот поток раз в 5 с). Радар Aster — один этот поток. `<symbol>@depth20@100ms` — формат Binance (`depthUpdate`, `b`/`a`), `!markPrice@arr@1s` |

Стакан и лучшие цены сходятся (40 с на шести символах: расхождений при старом стакане нет). У тихих контрактов (OKB) стакан приходит раз в несколько секунд — такие ноги честно получают «устарело», если лучшие цены с ним не совпали.

### Ключи: API-кошелёк, а не API-ключ [док 14.09.2026]

Актуальная документация Aster (futures v3) описывает только подпись кошельком: каждый приватный запрос несёт `user` (адрес основного кошелька), `signer` (адрес API-кошелька) и `nonce` в микросекундах (±60 с от часов сервера) и подписывается EIP-712 (`AsterSignTransaction`, chainId 1666) приватным ключом API-кошелька. API-кошелёк создаётся на https://www.asterdex.com/en/api-wallet.

В терминале поле «ключ» — адрес основного кошелька, поле «секрет» — приватный ключ API-кошелька. ccxt по умолчанию берёт `user` из адреса самого ключа (то есть ждёт ключ основного кошелька); адаптер закрепляет `user` за основным кошельком и отдельно указывает `signer`. Если введён ключ самого основного кошелька (адрес ключа совпал с `user`), проверка аккаунта ставит «вывод включён» и ключ не принимается. Права API-кошелька Aster не сообщает — предупреждение, как у Gate.

### Приватная часть [док, проверить пробной сделкой]

| Эндпоинт | Что делаем |
|---|---|
| `GET /fapi/v3/positionSide/dual` | `dualSidePosition` (one-way = `false`) |
| `GET /fapi/v3/balance` | USDT: `balance`, `availableBalance` |
| `GET /fapi/v3/accountWithJoinMargin` | `totalMarginBalance`, `availableBalance`, `totalInitialMargin` |
| `GET /fapi/v3/commissionRate?symbol=BTCUSDT` | `takerCommissionRate`, `makerCommissionRate` долями. Без проверки ключа терминал считает тейкер 0,035% |
| `GET /fapi/v3/positionRisk` | как `v2/positionRisk` у Binance |
| `POST /fapi/v3/leverage`, `POST /fapi/v3/marginType` | −4046 «менять не нужно» не ошибка |
| `POST /fapi/v3/order` | `MARKET`, `newClientOrderId` (`^[.A-Z:/a-z0-9_-]{1,36}$`), `newOrderRespType=RESULT`, `reduceOnly=true` на закрытии |
| `GET /fapi/v3/order?origClientOrderId=` | −2013 — ордера нет |
| `GET /fapi/v3/userTrades?orderId=`, `GET /fapi/v3/income?incomeType=FUNDING_FEE` | исполнения и фандинг |

Приватный вебсокет Aster терминал пока не использует.

---

## BingX Perpetual Swap (подключена 14.09.2026)

**Библиотека:** в ccxt биржа называется `bingx`, методы `swap_v2_public_*`, `swap_v2_private_*`. ccxt помечает запросы своим брокерским id (`X-SOURCE-KEY: CCXT`); у BingX есть пары, где брокерские ордера запрещены (`brokerState`), поэтому адаптер этот заголовок убирает. Официальная документация — одностраничное приложение; поля ниже взяты из его исходника (`bingx-api.github.io/docs-v3`).

### Публичная часть [проверено 14.09.2026]

| Эндпоинт | Что берём |
|---|---|
| `GET https://open-api.bingx.com/openApi/swap/v2/quote/contracts` | 1224 контракта; терминал берёт `currency` `USDT`, `status` 1 (25 — только закрытие, 5 — до листинга, 0 — выключен), `apiStateOpen` и `apiStateClose` `"true"` — 817. `quantityPrecision` — шаг количества в монетах контракта (у SOL поле `size` равно 1, а шаг 0,01 — `size` терминал не использует, как и ccxt), `tradeMinQuantity` (монеты) и `tradeMinUSDT` — минимумы, `pricePrecision`, `takerFeeRate` 0,0005. Множители в имени: `1000PEPE-USDT`, `10000SATS-USDT`, `1000000MOG-USDT`; `PEPE-USDT` снят. Около 590 контрактов `NC…2USD-USDT` — форекс, акции, сырьё; с криптобиржами они не пересекаются |
| `GET /openApi/swap/v2/quote/ticker` (все символы) | `bidPrice`, `askPrice`, `quoteVolume` — радар BingX, опрос раз в секунду |
| `GET /openApi/swap/v2/quote/premiumIndex` (все символы) | `markPrice`, `indexPrice`, опрос раз в 5 с |
| `GET /openApi/swap/v2/quote/bookTicker` | только с `symbol` (без него код 109400) |
| `GET /openApi/swap/v2/server/time` | `data.serverTime` |
| `wss://open-api-swap.bingx.com/swap-market` | **кадры сжаты gzip**; подписка `{"id","reqType":"sub","dataType":"BTC-USDT@depth20@100ms"}`; сервер присылает текст `Ping` и ждёт `Pong`. **Не больше 200 подписок на соединение** (201-я — код 80403), терминал держит до 50. Стакан: `data.bids`/`asks` — `[цена, количество в монетах контракта]`, `ts` |

**Лучшие цены берутся из REST, а не из вебсокета [проверено 14.09.2026].** Поток `<symbol>@bookTicker` отставал и стоял шире стакана: верх вебсокетного стакана совпал с REST-ценами 58 раз из 60, с `@bookTicker` — 14 раз. REST-тикер обновляется примерно так же, как REST `bookTicker`; у тихих альтов цена меняется раз в несколько секунд.

### Приватная часть [док, проверить пробной сделкой]

| Эндпоинт | Что делаем |
|---|---|
| `GET /openApi/v1/account/apiPermissions` | `permissions`: 1 спот, 2 чтение, 3 фьючерсы, 4 переводы, **5 вывод**, 7 переводы субаккаунтов; `ipAddresses` |
| `GET /openApi/swap/v1/positionSide/dual` | `dualSidePosition` `"true"`/`"false"` |
| `GET /openApi/swap/v3/user/balance` | USDT: `balance`, `equity`, `availableMargin`, `usedMargin`, `freezedMargin` |
| `GET /openApi/swap/v2/user/commissionRate` | `data.commission.takerCommissionRate` долями |
| `GET /openApi/swap/v2/user/positions` | `positionAmt` в монетах, `positionSide` LONG/SHORT, `avgPrice`, `markPrice`, `liquidationPrice` (0 — нет), `isolated`, `leverage`. Код 109500 — сбой запроса, а не «позиций нет» |
| `GET`/`POST /openApi/swap/v2/trade/marginType` | `ISOLATED`/`CROSSED`; адаптер меняет, только если режим другой |
| `POST /openApi/swap/v2/trade/leverage` | в one-way только `side=BOTH` |
| `POST /openApi/swap/v2/trade/order` | `MARKET`, `positionSide=BOTH`, `quantity` в монетах, `clientOrderID` (1–40 символов, **BingX приводит его к нижнему регистру** — id терминала и так строчные), `reduceOnly=true` на закрытии. Ответ — только `orderId`, поэтому сразу за ним читается статус |
| `GET /openApi/swap/v2/trade/order?clientOrderId=` | `status` NEW/PENDING/PARTIALLY_FILLED/FILLED/CANCELLED/FAILED, `executedQty`, `avgPrice`; 80016/80017 — ордера нет |
| `GET /openApi/swap/v2/trade/allFillOrders` | обязательны `tradingUnit` (`COIN`), `startTs`, `endTs`; у исполнений нет своего id — адаптер строит его из `orderId`, времени и порядкового номера. Если сумма `volume` совпадёт с исполненным количеством в токенах, а не в монетах контракта (для `1000PEPE`), адаптер пересчитает — единицы подтвердит пробная сделка |
| `GET /openApi/swap/v2/user/income?incomeType=FUNDING_FEE` | фандинг |

Приватный вебсокет BingX терминал пока не использует.

---

## Variational Omni (подключён только на чтение 14.09.2026)

- **Торгового API нет** [док 14.09.2026]: «The trading API is still in development, and is not yet available to any users». Пары с Variational видны в каталоге и на экране «Пары», открыть их нельзя.
- **Единственный эндпоинт** `GET https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats`: 553 рынка, ~280 КБ. Лимит 10 запросов за 10 секунд с одного IP — терминал делает один общий запрос раз в 2 секунды.
- **Поля:** `ticker` (голое имя, `1000PEPE` — цена за 1000 PEPE), `mark_price`, `volume_24h`, `funding_rate`, `quotes.base` (лучшие цены), `quotes.size_1k` и `quotes.size_100k` (средняя цена сделки на $1 тыс. и $100 тыс.), у крупных `size_1m`, `quotes.updated_at` с наносекундами. Индекса нет — с другими биржами сравнивается `mark_price` против их индекса.
- **Стакана нет:** терминал строит из двух котировок стакан из двух уровней, проход по которому даёт ровно среднюю цену Variational на $1 тыс. и на $100 тыс.; больше $100 тыс. — «глубины не хватает».
- **Котировки из кэша [проверено 14.09.2026]:** возраст по `updated_at` 41–105 с, медиана около минуты; три запроса с интервалом 3 с вернули один и тот же снимок. Документация: «The bid/ask price may be cached for up to 600 seconds». Минутные цены против живых стаканов дают ложные вилки, поэтому при пороге свежести 30 с (`[exchanges.variational] max_quote_age_ms`) пары с Variational в ленту и историю не попадают.
- **Комиссий нет** [док]: «There are no trading fees on Omni», заработок — в спреде котировок.
- **User-Agent:** Cloudflare перед API отвечает 403 на `Python-urllib`; aiohttp проходит, адаптер представляется `terminal-ludik/0.1`.

---

## Остальные биржи

Публичные эндпоинты тикеров, которые работали в парсере [прод]. Для терминала у каждой биржи дополнительно нужны bid/ask, стакан и приватная часть.

| Биржа | id в ccxt | Тикеры (все символы) | Цена в парсере | Объём 24ч в парсере | Заметки |
|---|---|---|---|---|---|
| Bybit | `bybit` | `GET https://api.bybit.com/v5/market/tickers?category=linear` | `lastPrice` | `turnover24h` | |
| OKX | `okx` | `GET https://www.okx.com/api/v5/market/tickers?instType=SWAP` | `last` | `volCcy24h` | у SWAP `volCcy24h`, похоже, в базовой монете, а не в $ — парсер считал его долларами [проверить]. Торгуется контрактами |
| Bitget | `bitget` | `GET https://api.bitget.com/api/v2/mix/market/tickers?productType=USDT-FUTURES` | `lastPr` | `quoteVolume` | |
| Phemex | `phemex` | `GET https://api.phemex.com/md/v3/ticker/24hr/all?type=Perpetual` | `lastRp` | `turnoverRp` (в парсере делится на 1e8) | масштаб поля [проверить] |
| Hyperliquid | `hyperliquid` | `POST https://api.hyperliquid.xyz/info` `{"type":"metaAndAssetCtxs"}` | `midPx` | `dayNtlVlm` | префикс `k` = ×1000. Торговля через API-кошелёк без права вывода, а не API-ключ |
| KuCoin Futures | `kucoinfutures` | (в парсере только фандинг) `GET https://api-futures.kucoin.com/api/v1/contracts/active` | — | — | `XBT` = BTC, суффикс `USDTM` |
