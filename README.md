# Atlas — personal AI finance board (phases 1–2)

- **Phase 1** is the foundation from the spec: the **Bookkeeper**, the **Risk Officer**, the **shared report format** and a **text-only Atlas** you can chat with. It also includes a minimal **Trader** that fills approved trades on paper.
- **Phase 2** adds the first analysts: the **Advisor** (drift, goal pacing, rebalance plans) and **Market Watch** (headlines, the 24-hour pause, why things moved, the week's calendar).

Filings Research, Value Research, Tax & Accounts, Savings Coach, Scout and the Auditor come later. They plug into the same report format and the same read-only books.

```
              you (CLI / local API)
                 │ approve, rules, kill switch
   ┌─────────────┴──────────────┐
   │ Atlas chat (read-only tools)│  ← Claude, or offline responder
   └─────────────┬──────────────┘
        reads    │   reads reports
   ┌─────────────┴──────────────┐     ┌──────────────────────┐
   │ Books (read-only view)      │◄────┤ ReportDesk (shared   │
   │  Bookkeeper = Books + writes│     │ format, validated)   │
   └──┬─────────────┬────────────┘     └──────────▲───────────┘
      │ prices/FX   │ holdings                    │ breach reports
 PriceService    Broker slot            Risk Officer (rules in code,
 EODHD·Finnhub   Manual·Paper·SnapTrade  kill switch, veto)
 BoC·Sample      (stub)                         │ pass/block
                                         Trader → PaperBroker → fills → Bookkeeper
```

## Quick start

```bash
pip install -e ".[dev]"
atlas init            # creates data/atlas.db, loads the sample book
atlas brief           # one-screen status
atlas marketwatch --morning   # news, calendar, Bank of Canada rate, USD/CAD
atlas advisor         # drift, goal checks, and a rebalance plan for your approval
atlas plan            # see the plan;  atlas plan approve PLAN_ID
atlas chat            # talk to Atlas  (or: atlas chat "how are my goals?")
pytest                # 75 tests
```

With no keys, everything runs on **clearly labelled SAMPLE prices**, and the chat uses an offline rule-based responder. Copy `.env.example` to `.env` and add keys to go live:

| Key | Used for |
|---|---|
| `EODHD_API_KEY` | TSX + US quotes, daily history, news for Canadian names, FX fallback, fundamentals (later) |
| `FINNHUB_API_KEY` | US live quotes, US company news |
| *(none)* | Bank of Canada Valet: official daily USD/CAD |
| `ANTHROPIC_API_KEY` | Atlas chat on Claude (`claude-opus-5-5`; `atlas chat --engine claude` also picks up an `ant auth login` profile), and Market Watch's headline judgment (structured JSON at low effort; `ATLAS_LLM_SPECIALISTS=0` turns it off) |

Once any market-data key is set, sample prices are never used as a silent fallback. A failed fetch keeps the last real price, and the Bookkeeper flags it as stale.

## The sample book

These are illustrative holdings across Canadian account types. The prices are placeholders (`atlas/prices/sample.py`).

| Account | Holdings |
|---|---|
| TFSA | XEQT.TO, VFV.TO, cash |
| RRSP | VOO (US), ZAG.TO, XEF.TO, CAD + USD cash |
| FHSA (house fund) | XIC.TO, cash |
| Non-registered | NVDA, AAPL, RY.TO, cash |

The sample reproduces the mockup's story. NVDA is 13.6% of an $88,190 portfolio, so the Risk Officer reports a breach of the 10% single-stock cap. US stocks are 7 points over target. A trim of 15 shares passes every rule and fills on paper.

Replace it with your own holdings:

```bash
atlas import my-holdings.csv     # replaces each account's positions with the file's
atlas catalog my-instruments.csv # optional: names, ETF look-through mix, sector, fee
```

The import CSV takes these columns: `account_id, account_name, account_type, symbol, exchange, quantity, avg_cost, currency`. Cash goes in as `symbol=CASH`, with the amount in `quantity`. Common broker-export headers such as "Account Type", "Book Value (Market)" and "Market Value" are recognised too. Check the import flags after loading, because header mapping is best-effort. Instruments missing from the catalogue get a guessed mix and are flagged `unclassified`.

## The pieces

### Bookkeeper (`atlas/bookkeeper.py`)
- Exact `Decimal` money, stored as text so SQLite never rounds it through a float. Totals are in CAD.
- Produces holdings and values, gains, mix vs target (with ETF look-through, e.g. XEQT ≈ 25% CA / 45% US / 30% intl), goal progress and net-worth snapshots.
- Flags missing or stale prices, missing FX, sample data, unclassified instruments, negative quantities or cash, totals or weights that don't add up, and import rows whose quantity × price ≠ market value.
- **Write boundary:** every agent receives `Books`, the read-only view. Only `Bookkeeper` (imports, prices, snapshots) and the Trader's fills (`Bookkeeper.apply_fill`) change holdings and cash.
- Its daily report is always `NO VIEW`: facts only.

### Broker slot (`atlas/brokers/`)
All brokers share one interface: `list_accounts`, `list_holdings`, `list_orders`, `place_order`.
- `ManualBroker`: CSV import. It can't place orders; you trade in your broker's app and re-import.
- `PaperBroker`: simulated limit-only DAY orders at real prices, with 5 bp slippage that never crosses the limit. Resting orders are re-checked on each price refresh and expire at the 16:00 ET close. Paper accounts (`paper-tfsa`, …) start as a copy of the real book (`atlas paper reset`).
- `SnapTradeBroker`: a stub. Trading stays off until SnapTrade is confirmed for your accounts and within Wealthsimple's terms.

### Price adapters (`atlas/prices/`)
The PriceService routes each request to a source:

| Request | Source order |
|---|---|
| US quotes | Finnhub → EODHD |
| TSX quotes | EODHD |
| News | Finnhub (US) / EODHD (Canada) |
| FX | Bank of Canada → EODHD |

API keys never appear in error messages. Finnhub's key goes in a header, not the URL.

### Risk Officer (`atlas/risk.py`)
These rules are in plain code. Each pre-trade check returns pass or block per rule, with numbers.

| Rule | Default |
|---|---|
| Max share in any one stock (diversified ETFs exempt) | 10% |
| Max share in any one sector | 30% |
| Minimum cash floor | 5% |
| No trades within N hours of a major headline | 24h |
| Trader daily / monthly loss limit | $500 / $2,000 |
| Limit price within N% of the last price (fat-finger guard) | 5% |

These guards are always on and can't be configured: limit orders only, fresh price and FX required, enough cash or shares in the account.

- **Only you loosen.** Any agent may tighten a rule. Loosening or disabling one with any actor other than `user` raises `RuleChangeRefused`, and the attempt is logged.
- **Kill switch:** `atlas kill on|off`. Anyone can engage it, and engaging cancels open orders. Only you can release it. `ATLAS_KILL_SWITCH=1` forces it on regardless of the database.
- **Continuous scan:** the Risk Officer catches rules broken by price moves alone and files a `TRIM` report with sourced evidence.

### Shared report format (`atlas/reports.py`)
Reports carry `agent, as_of, subject, finding, stance (BUY|ADD|HOLD|TRIM|SELL|NO VIEW), confidence 0–1, urgency, evidence[{claim, source}], wrong_if, next_check`. Both spec rules are enforced:
- A stance without `wrong_if` is **sent back** (`ReportSentBack`).
- A claim without a source **can't justify a trade**. `report.can_support_trade` is checked by the Trader, and an agent's proposal must cite at least one report with sourced evidence.

`dissent()` finds the minority view so Atlas can't bury it. `atlas schema` prints the JSON schema for future Claude-based specialists.

### Trader (`atlas/trader.py`)
The flow is `propose` → Risk check → you `approve` (re-checked) → execute on the paper broker (checked one last time). An approval is bound to a hash of the exact order, so anything changed after approval is refused. Every step is written to `audit_log`.

### Atlas chat (`atlas/chat/`)
- Atlas uses Claude with **read-only tools**: overview, holdings, allocation, goals, risk status, a `check_trade` dry run, reports with dissent, news, calendar events, pending plans, orders, history and quote.
- No tool trades, approves, proposes or changes a rule. When you ask for one of those, Atlas tells you the command to run yourself.
- Requests use adaptive thinking (the model default), `effort=medium`, prompt caching and server-side refusal fallback (`fallbacks: "default"`).
- Without a key, an offline responder answers the common questions from the same tools.

### Market Watch (`atlas/agents/market_watch.py`)
- **Coverage:** follows what you own (in either book) and your watchlist (`atlas watch add XBB.TO`).
- **Headline verdicts:** every headline is classified as relevant or not and major or not, with a category. Irrelevant stories are kept but never reported. Claude makes the judgment when a key is set; otherwise keyword rules do.
- **The 24-hour rule:** a major headline sets `is_major`, which the Risk Officer's cool-down reads, and Market Watch files a high-urgency report.
- **Injection safety:** Claude can *add* a major flag but can never remove one the keyword rules raised. Text planted in a headline therefore can't lift the pause or hide the story.
- **"Why did this move":** a stock moving 4% or more in a day (2% for an ETF) gets an explanation. Claude may only cite headlines it was given; invented ids are dropped. Without a key, a rules-based explanation is used.
- **Morning scan:** earnings dates for holdings and watchlist names (Finnhub for US, EODHD for Canada), Canadian and US economic releases (EODHD), the Bank of Canada overnight rate (Valet `V39079`), and any USD/CAD move of 0.75% or more.
- **Stance:** its reports are facts (`NO VIEW`).

### Advisor (`atlas/agents/advisor.py`)
- **Drift:** any bucket outside its band ("US stocks 7.0 points over target").
- **Goal checks:** projected value at the goal date, from the goal's monthly contribution (`atlas goal house 800`) and your return assumptions (`atlas assume us_equity 5.5`; defaults are 6% stocks, 3.5% bonds, 2.5% cash). The report gives the return the goal actually needs as its `wrong_if`. It flags a goal less than 3 years out that is mostly stocks.
- **Rebalance plans:**
  - Rule breaches are handled first: a stock over the cap is trimmed to half a point under it. Drift outside the bands comes second.
  - Sells come from registered accounts before taxable ones. Selling in a taxable account shows the capital gain it would realize.
  - Buys go to underweight buckets using preferred ETFs (XIC, VFV/VOO, XEF, ZAG), in whole shares above a minimum trade size.
  - It never buys stocks inside an account tied to a goal under 3 years away.
  - Each leg's limit is 0.25% through the last price, so it fills.
- **Approval:** plan legs go to the Trader in the paper book and wait for `atlas plan approve`. The Risk Officer checks the legs **as a sequence**, so a buy can use cash an earlier sell raises.
- **Limits on the Advisor:** it can't approve, can't change targets or assumptions, proposes at most one plan a day, cites only sourced reports, and pauses advice whenever the books have data errors.

### Scheduled jobs (`atlas/jobs.py`, Toronto time)
- `prices`: every 5 minutes, 09:30–16:00 on weekdays, plus one run at 16:05. It refreshes prices and FX, works open paper orders and runs the Risk Officer scan.
  If any bucket is outside its band, the Advisor runs straight away.
- `morning_scan`: 08:00 on weekdays (Market Watch with the calendar).
- `market_watch`: every 15 minutes in market hours.
- `daily_close`: 16:45 on weekdays. It takes snapshots and files the Bookkeeper's report.
- `advisor`: 17:00 on weekdays.

Reports aren't re-filed on every price tick: each finding goes in once a day unless something material changes (for example, a breach moves by a point or more).

Start the scheduler with `atlas jobs start`, or run one job with `atlas jobs run prices`.

### Local API (`atlas serve`)
FastAPI on `127.0.0.1:8787`, as groundwork for the phone app in phase 4. Read endpoints are open. Write endpoints (proposals, approve, plan approve, kill switch) require `X-Atlas-Token` matching `ATLAS_API_TOKEN` and are disabled when no token is set.

## Commands

```
atlas init | import CSV | catalog CSV | brief | holdings [--book paper] | allocation
atlas chat [question] [--engine auto|claude|offline]
atlas prices | news | marketwatch [--morning] | advisor | events [--days N]
atlas plan [show ID | approve ID | dismiss ID]
atlas watch [add|remove TICKER] | assume [BUCKET PCT] | goal GOAL_ID MONTHLY
atlas rules [set KEY VALUE | enable KEY | disable KEY]
atlas kill on|off --reason "..."
atlas propose SELL 15 NVDA.US --account paper-nonreg --limit 189 --why "trim to cap"
atlas approve ID | dismiss ID | orders | reports [--subject NVDA.US] | schema
atlas paper reset | jobs start | jobs run NAME | serve
```

An older database is upgraded in place on start-up (columns are only ever added).

## Known limits
- Exchange holidays aren't modelled. The price job just re-reads the last close.
- Cost basis for USD holdings is converted at today's FX rate, not the rate on the purchase date. Tax-exact ACB is a job for Tax & Accounts.
- Without a Claude key, headline judgment is keyword-based. It is conservative for the pause, but less precise on relevance.
- USD sale proceeds stay as USD cash in the paper book. Plans note this, and buy only with cash already in the matching currency.
- Goal projections are only as good as the return assumptions you set. They are not forecasts.
- Paper trading doesn't simulate currency conversion. A USD buy needs USD cash in that account.
- Header mapping for broker exports hasn't been tested against a real Wealthsimple export yet.

Atlas gives information, not licensed financial or tax advice.
