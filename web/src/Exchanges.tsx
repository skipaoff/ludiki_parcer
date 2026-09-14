// VFP: Settings → Exchanges — enter or replace API keys, run the account check, read what the exchange reported.
// Changes when: the key check reports new facts or the key workflow changes.
// Anti-goal:
// 1. Keeping a typed secret in the page after it was sent — the form is cleared on every attempt.
// 2. Showing a raw key — only the masked key from the terminal is ever displayed.

import { useCallback, useEffect, useState } from "react";
import { clock } from "./format";
import { apiGet, apiSend } from "./session";
import type { CheckResult, ExchangeDetails, ExchangeState } from "./types";

const VERDICT_TEXT: Record<string, string> = {
  withdrawals_enabled: "у ключа включён вывод средств — ключ не принят, выпустите ключ без вывода",
  futures_disabled: "у ключа нет права на фьючерсы",
  reading_disabled: "у ключа нет права на чтение",
  hedge_mode: "на бирже включён режим hedge — переключите режим позиций на one-way в настройках фьючерсов",
  check_incomplete: "проверка не завершена — см. ошибки ниже",
  withdrawals_unknown: "биржа не сообщает права ключа — убедитесь сами, что вывод у ключа выключен",
  position_mode_unknown: "режим позиций не получен",
  no_ip_restriction: "ключ без привязки к IP — у части бирж такие ключи ограничены по сроку",
  no_free_balance: "нет свободного USDT на фьючерсном счёте",
  fees_unknown: "комиссии аккаунта не получены",
  clock_offset: "часы компьютера расходятся с биржей — включите синхронизацию времени Windows",
};

export function exchangeTitle(exchange: { name: string; demo: boolean }): string {
  return exchange.name.toUpperCase() + (exchange.demo ? "·ДЕМО" : "");
}

export function signedMs(value: number): string {
  const sign = value > 0 ? "+" : value < 0 ? "−" : "";
  const abs = Math.abs(value);
  return abs >= 1000 ? `${sign}${(abs / 1000).toFixed(1)}с` : `${sign}${abs}мс`;
}

export function ExchangesSettings({ token, live }: { token: string; live: ExchangeState[] | undefined }) {
  const [details, setDetails] = useState<ExchangeDetails[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const signature = (live ?? []).map((exchange) => `${exchange.name}:${exchange.keys}`).join("|");

  const reload = useCallback(() => {
    apiGet<{ exchanges: ExchangeDetails[] }>("/api/exchanges", token)
      .then((data) => {
        setDetails(data.exchanges);
        setError(null);
      })
      .catch((reason: Error) => setError(reason.message));
  }, [token]);

  useEffect(reload, [reload, signature]);

  if (error) return <p className="muted">Не удалось получить биржи: {error}</p>;
  if (!details) return <p className="muted">загрузка…</p>;

  return (
    <>
      {details.map((exchange) => (
        <ExchangeCard
          key={exchange.name}
          token={token}
          exchange={exchange}
          live={live?.find((item) => item.name === exchange.name)}
          onChange={(updated) => setDetails((current) => current?.map((item) => (item.name === updated.name ? updated : item)) ?? null)}
        />
      ))}
      <p className="muted">
        Ключи хранятся в Диспетчере учётных данных Windows и в интерфейс не возвращаются. Выпускайте ключи только с правами на
        чтение и фьючерсы, без вывода.
      </p>
    </>
  );
}

function ExchangeCard({
  token,
  exchange,
  live,
  onChange,
}: {
  token: string;
  exchange: ExchangeDetails;
  live: ExchangeState | undefined;
  onChange: (exchange: ExchangeDetails) => void;
}) {
  const [editing, setEditing] = useState(exchange.key_masked === null);
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const link = live ?? exchange;

  useEffect(() => {
    if (exchange.key_masked === null) setEditing(true);
  }, [exchange.key_masked]);

  const run = async (label: string, action: () => Promise<ExchangeDetails>) => {
    setBusy(label);
    setMessage(null);
    try {
      onChange(await action());
      return true;
    } catch (reason) {
      setMessage((reason as Error).message);
      return false;
    } finally {
      setBusy(null);
    }
  };

  const save = async () => {
    const key = apiKey;
    const secret = apiSecret;
    setApiKey("");
    setApiSecret("");
    const saved = await run("сохранение", () =>
      apiSend<ExchangeDetails>("PUT", `/api/exchanges/${exchange.name}/keys`, token, { api_key: key, api_secret: secret }),
    );
    if (saved) {
      setEditing(false);
      await run("проверка", () => apiSend<ExchangeDetails>("POST", `/api/exchanges/${exchange.name}/check`, token));
    }
  };

  const remove = async () => {
    if (!window.confirm(`Удалить ключи ${exchangeTitle(exchange)} из Диспетчера учётных данных?`)) return;
    await run("удаление", () => apiSend<ExchangeDetails>("DELETE", `/api/exchanges/${exchange.name}/keys`, token));
  };

  const check = () => run("проверка", () => apiSend<ExchangeDetails>("POST", `/api/exchanges/${exchange.name}/check`, token));

  if (exchange.read_only) {
    return (
      <section className="exchange-card">
        <h2>
          {exchangeTitle(exchange)}{" "}
          <span className={link.link === "down" ? "blink" : "muted"}>
            {link.link === "up" && link.ping_ms != null ? `· данные ${link.ping_ms}мс` : link.link === "down" ? "· нет связи" : "· проверка связи…"}
          </span>
        </h2>
        {link.link === "down" && exchange.probe_error && <p className="muted">{exchange.probe_error}</p>}
        <p className="muted">
          Только наблюдение: у Variational пока нет торгового API. Вилки с Variational видны в ленте и записываются в историю,
          но открыть их через терминал нельзя. Котировки обновляются раз в несколько секунд и не содержат стакана — цена берётся
          на размер $1 тыс. и $100 тыс.
        </p>
      </section>
    );
  }

  return (
    <section className="exchange-card">
      <h2>
        {exchangeTitle(exchange)}{" "}
        <span className={link.link === "down" ? "blink" : "muted"}>
          {link.link === "up" && link.ping_ms != null
            ? `· связь ${link.ping_ms}мс · часы ${signedMs(link.clock_offset_ms ?? 0)}`
            : link.link === "down"
              ? "· нет связи"
              : "· проверка связи…"}
        </span>
      </h2>
      {link.link === "down" && exchange.probe_error && <p className="muted">{exchange.probe_error}</p>}

      <div className="row">
        <span>ключ</span>
        <span>{exchange.key_masked ?? <span className="muted">не добавлен</span>}</span>
      </div>

      {editing ? (
        <form
          className="key-form"
          autoComplete="off"
          onSubmit={(event) => {
            event.preventDefault();
            void save();
          }}
        >
          <label>
            <span>API key</span>
            <input type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} spellCheck={false} />
          </label>
          <label>
            <span>Secret</span>
            <input
              type="password"
              value={apiSecret}
              onChange={(event) => setApiSecret(event.target.value)}
              spellCheck={false}
            />
          </label>
          <div className="actions">
            <button className="action" type="submit" disabled={busy !== null || !apiKey || !apiSecret}>
              [СОХРАНИТЬ И ПРОВЕРИТЬ]
            </button>
            {exchange.key_masked && (
              <button className="action" type="button" onClick={() => setEditing(false)}>
                [ОТМЕНА]
              </button>
            )}
          </div>
        </form>
      ) : (
        <div className="actions">
          <button className="action" onClick={() => void check()} disabled={busy !== null || exchange.keys === "checking"}>
            [ПРОВЕРИТЬ]
          </button>
          <button className="action" onClick={() => setEditing(true)} disabled={busy !== null}>
            [ЗАМЕНИТЬ КЛЮЧИ]
          </button>
          <button className="action" onClick={() => void remove()} disabled={busy !== null}>
            [УДАЛИТЬ]
          </button>
        </div>
      )}

      {busy && <p className="muted blink">{busy}…</p>}
      {message && <p className="level-warning">{message}</p>}
      {exchange.check && <CheckView result={exchange.check} />}
    </section>
  );
}

function CheckView({ result }: { result: CheckResult }) {
  const facts = result.facts;
  const yesNo = (value: boolean | null, yes = "да", no = "нет") => (value === null ? "неизвестно" : value ? yes : no);
  const money = (value: string | null) => (value === null ? "—" : Number(value).toLocaleString("ru-RU", { maximumFractionDigits: 2 }));
  const pct = (value: string | null) => (value === null ? "—" : `${Number(value).toFixed(4)}%`);

  return (
    <div className="check">
      <div className="row">
        <span>итог проверки · {clock(result.checked_at_ms)}</span>
        <span className="action">{result.accepted ? (result.warnings.length ? "принят с замечаниями" : "принят") : "НЕ ПРИНЯТ"}</span>
      </div>
      {result.blocking.map((code) => (
        <p key={code} className="level-warning">
          ✕ {VERDICT_TEXT[code] ?? code}
        </p>
      ))}
      {result.warnings.map((code) => (
        <p key={code}>! {VERDICT_TEXT[code] ?? code}</p>
      ))}
      <div className="row">
        <span>USDT на фьючерсах: кошелёк / свободно</span>
        <span>
          {money(facts.wallet_usdt)} / {money(facts.available_usdt)}
        </span>
      </div>
      <div className="row">
        <span>права ключа: чтение / фьючерсы / вывод / привязка к IP</span>
        <span>
          {yesNo(facts.permissions.reading)} / {yesNo(facts.permissions.futures)} / {yesNo(facts.permissions.withdrawals)} /{" "}
          {yesNo(facts.permissions.ip_restricted)}
        </span>
      </div>
      <div className="row">
        <span>режим позиций</span>
        <span>{facts.one_way_position_mode === null ? "неизвестно" : facts.one_way_position_mode ? "one-way" : "hedge"}</span>
      </div>
      <div className="row">
        <span>комиссии: тейкер / мейкер</span>
        <span>
          {pct(facts.taker_fee_pct)} / {pct(facts.maker_fee_pct)}
        </span>
      </div>
      <div className="row">
        <span>пинг / часы</span>
        <span>
          {facts.ping_ms ?? "—"}мс / {facts.clock_offset_ms === null ? "—" : signedMs(facts.clock_offset_ms)}
        </span>
      </div>
      {facts.errors.map((text) => (
        <p key={text} className="muted">
          ошибка: {text}
        </p>
      ))}
      {facts.notes.map((text) => (
        <p key={text} className="muted">
          замечание: {text}
        </p>
      ))}
    </div>
  );
}
