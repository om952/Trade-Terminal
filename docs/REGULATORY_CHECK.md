# Phase 5 regulatory check: retail algo trading through the Upstox API

Checked 21 November 2026 against the primary documents listed at the end. This is a reading of the circulars for one purpose: deciding whether, and how, Trade Terminal may send real orders. It is not legal advice. Where the documents are ambiguous, this says so, and the questions to settle before going live are listed at the end.

## The short answer

Trade Terminal, run by you on your own Upstox account, is a **"tech-savvy retail investor" algo within the order-per-second threshold**. That is the lightest category in the framework:

- **No registration.** It needs no registration with the exchange and no algo ID of its own. The broker tags its orders with the generic algo ID.
- **No empanelment.** It needs no empanelment as an algo provider and no Research Analyst registration.
- **No mock sessions.** It is excused from the monthly mock trading sessions.

That category comes with conditions. Each one changes what Phase 5 has to build:

1. **Every order goes out from a static IP registered with Upstox.** One primary and one optional secondary, changed at most once a calendar week.
2. **Fewer than 10 orders per second per exchange,** counted on the broker's clock. A portfolio rebalance fired in one burst would break this.
3. **No market orders.** NSE rejects algo market orders outright, and every order from an API counts as an algo order. Entries, exits and stops must be limit orders, and stops must be stop-limit, not SL-M.
4. **OAuth with two-factor login, and a session that ends every day.** The Upstox token dies at 03:30 IST, so a person logs in each trading morning.
5. **You are responsible for the strategy's logic and its outcome.** One NSE text also puts the risk checks on you, while a later one puts them on the broker. Phase 5 builds them either way (section 6).

The category also has limits. It covers you, and only for your own account. The registered-algo family provision covers self, spouse, dependent children and dependent parents. **Offering Trade Terminal to anyone else as a service would make it an "algo provider".** That role needs exchange empanelment, and a black-box algo needs SEBI Research Analyst registration as well. The repository is public, so section 2 deals with that.

The framework applies to every broker from **1 April 2026** (SEBI circular of 30 September 2025, para 8). Upstox enforces the static IP rule from that date. SEBI's algorithmic-trading listing shows nothing newer than that circular. NSE's consolidated circular of 30 April 2026 (NSE/INVG/73992) restates the same rules.

## 1. The documents, in order

| Date | Document | What it does |
|---|---|---|
| 4 Feb 2025 | SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/0000013 | Sets the retail algo framework. |
| 5 May 2025 | NSE/INVG/67858 | Implementation standards: static IP, the 10 orders per second threshold, tagging. |
| 22 Jul 2025 | NSE/INVG/69255, Annexure I | Operational rules: categories, generic algo ID "99999", 16 pre-trade risk checks. |
| 24 Jul 2025 | NSE/INVG/69289 | Corrigendum: order identification (NNF ID 444444444444 for client direct API). |
| 24 Jul 2025 | NSE/FAOP/69296 | F&O: algo market orders rejected by the exchange from 4 Aug 2025. |
| 19 Sep 2025 | NSE/INVG/70309 | Corrigendum: algo provider empanelment criteria (ISO 27001, VAPT). |
| 30 Sep 2025 | SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/132 | Glide path for brokers; full applicability from 1 Apr 2026. |
| 3 Nov 2025 | NSE FAQ on retail algo | Static IP only for tech-savvy investors; no market orders; no mock sessions for them. |
| 11 Mar 2026 | Upstox: Market Price Protection (beta) | MARKET and SL-M orders without protection are rejected. |
| 1 Apr 2026 | Upstox: algo trading circular | Static IP required for order APIs; rate limits. |
| 30 Apr 2026 | NSE/INVG/73992 | Consolidated NNF circular: restates the above and replaces NSE/MSD/67753. |
| Aug 2026 | SEBI study, individual traders in equity derivatives, FY25–FY26 | Context for the risk disclosure, not a rule. |

## 2. Tool or provider

**What the circulars say.**
- **Brokers and providers:** for algo trading through APIs, brokers are the principal, and any "algo provider or fintech/vendor" is the broker's agent (SEBI 2025/0000013, para 5.I.a). An algo provider is "any fintech / vendor providing algo facility through the usage of API" (NSE/INVG/69255, para 3). Every algo provider "shall require to be empaneled with Exchanges" (SEBI para 5.III.a).
- **Self-built algos:** "Algos developed by tech-savvy retail investors themselves, using programming knowledge, shall also be registered with the Exchange, through their broker, only if they cross the specified order per second threshold." The same registered algo may be used "for their family (but not for other investors)" (SEBI para 5.I.c).
- **Categories:** NSE's developer categories separate "Vendor Algo", "Trading Member (In-House) Algo", "Tech Savvy retail investor (beyond OPS threshold)" and "Tech Savvy retail investor (within OPS threshold)" (NSE/INVG/69255, para 2.4.2). Every algo must be registered with "the only exception of Tech Savvy retail investor (within OPS threshold)" (para 8.2.1).

**Where Trade Terminal sits.** You run it, on your machine, on your account. You see and approve every strategy's full logic: the read-back, the JSON spec and the backtest. That makes it a tech-savvy investor's own white-box algo, within the threshold as long as section 4's cap holds.

**Ambiguities to be aware of:**

- **The AI writes the strategy.** The circular says algos developed by investors "using programming knowledge". You don't hand-write each strategy; the builder's model drafts it and you approve it. The logic is still disclosed in full and replicable, and nobody else supplies it. I read this as your own white-box algo, but no document addresses AI-drafted strategies.
- **The public repository.** Publishing the source code is not "providing the facility to place algo orders with brokers". Nobody routes orders through you, and anyone who runs the code would be running their own instance on their own account. The line is crossed by offering a service: a hosted instance, signups, charging for it, or supplying strategies to other people's accounts. Two further rules apply if that ever happens:
  - SEBI's 2 September 2022 circular on performance claims by unregulated algo platforms. The README makes no return claims today; keep it that way.
  - A black-box strategy would need the provider to register as a Research Analyst (SEBI para 5.V).

  Take advice before Trade Terminal is used by anyone outside your family.

## 3. Registration and the algo ID

- **Tagging:** all orders received through a client API count as algo orders and must be tagged (NSE/INVG/69255, para 2.8; FAQ question 8). This holds even under the threshold.
- **Under the threshold, the broker tags them.** It sends the generic algo ID "99999" with the 13th NNF digit "0" (NSE/INVG/69255, para 8.2.4). The client sends nothing extra.
- **Upstox header:** the `X-Algo-Name` header "is optional and only required if you have an exchange-approved algo strategy" (Upstox Place Order V3).
- **One API key:** where a client has several API keys, "non-registered algos are run only through one of the predefined API keys" (NSE/INVG/67858, A.4). Trade Terminal uses one key, which satisfies this.
- **No mock sessions:** individual tech-savvy clients "are not required to participate in the monthly mock trading sessions" (NSE FAQ question 9; NSE/INVG/73992).

**Phase 5 needs:** no registration work. Keep one Upstox app and API key for trading.

## 4. The order-rate threshold

- **The threshold:** "not exceeding 10 orders per second per exchange/segment" (NSE/INVG/67858, F). It "will be applied basis the calendar clock second of the broker server" (B.2), and exchanges may change it "after due notice to the market".
- **Over the threshold:** the broker "shall reject/not accept/not process any orders exceeding the OPS limit" (B.5).
- **Upstox's own limits:** 10 per second, 500 per minute and 2,000 per 30 minutes for an unregistered app. Place, modify, cancel, multi-order and GTT calls count together. Exceeding them "might result in temporary suspension of access."
- **The wording differs:** the text varies between "below" (B.2) and "not exceeding" (F). Design for well under 10.

**Phase 5 needs:**
- **A single limiter** in front of every order call (place, modify, cancel), set at **5 per second** with at least 150 ms between calls, plus the per-minute and per-30-minute ceilings.
- **Paced rebalances.** A rebalance that sells 10 holdings and buys 10 is 20 orders, so it must be spread over several seconds. The engine's "sells first" order already allows this.
- **Sliced orders count.** Upstox's `slice` option turns one request into several exchange orders. Either count each slice or slice in our own code.
- **The limiter is a safety device.** Hitting it is a stop condition, not a queue to drain. It feeds the runaway check in section 6.

## 5. Static IP

- **Mandatory for self-built algos:** "The need for static IP is mandatory for API access for client generated algos … In case of client generated algos, the static IP shall be that of the client" (NSE/INVG/67858, A.5). Per the NSE FAQ, a client static IP is needed "only in case of Tech savvy Investor using API" (questions 3 and 6).
- **One primary and an optional secondary** (A.2).
- **Changes:** "not more than once a calendar week" (A.6). Upstox invalidates existing access tokens when the IP changes.
- **One client per IP,** except clients of one family, on a written request to the broker (A.7).
- **Where your algo runs:** a tech-savvy client hosts the algo "using a static IP at their end" (NSE FAQ question 5). Only vendor algos must run on broker servers.
- **What Upstox restricts:** placing, modifying and cancelling orders, multi-orders, GTT and "Exit All Positions". Holdings, positions, funds and historical candles "are not restricted by Static IP". Upstox also has `GET` and `PUT /v2/user/ip` to read and set the IPs.

**Phase 5 needs:** a decision on P5.6. The order calls must leave from the registered IP. The options:

| Option | What runs where | Trade-off |
|---|---|---|
| Static IP from your home ISP | Everything on your machine, as today | The cheapest way to stay local. Your machine must be on and online during market hours. ISP outages stop trading, so the watchdog (P5.3) matters more. |
| Mumbai VPS runs the whole app | App, data and scheduler on the VPS | Always on. Remote access makes API authentication and TLS mandatory (the plan's P5.6). Data and credentials move off your machine. |
| Order calls tunnelled through a VPS | App local; only Upstox order traffic leaves through a WireGuard tunnel to a VPS with a static IP | Keeps data and keys local and gives a fixed egress IP. One more moving part, and the tunnel failing must fail closed. |

Two checks either way: confirm with Upstox that a VPS or tunnel egress IP is acceptable (question 3), and make the app verify its egress IP against `GET /v2/user/ip` before the open, refusing to trade on a mismatch.

## 6. Orders: types and risk checks

**Market orders are out.**
- **The rule:** "Algo orders with order type as Market Order are not permitted" (NSE/INVG/73992, para 8.1.12, carried over from NSE/MSD/67753, 8.1.1.12). Risk check 5 adds: "Members are required to adhere to the Market Price Protection check, by not placing any algorithmic orders on the Exchange as a market order" (NSE/INVG/69255, 11.1).
- **Enforcement:** NSE rejects algo market orders at the exchange in the cash segment (NSE/CMTR/68802, 30 Jun 2025) and in F&O (NSE/FAOP/69296, from 4 Aug 2025).
- **Upstox:** a MARKET or SL-M order with `market_protection: 0` is rejected, with code UDAPI1158: "Market orders are not allowed. Try placing a limit order." The default `-1` applies automatic protection under a beta feature (from 11 Mar 2026). Upstox's pages don't say whether a protected MARKET order counts as compliant.

**Recommendation:** Trade Terminal sends its own **LIMIT** orders and **SL** (stop-limit) orders, priced from the last price plus a set band, inside the exchange price bands. It should not rely on the market-protection beta. That keeps the price known and lets paper trading model the same order.

**This changes the engine.** Paper and backtest fills are market fills today: at the next open for the portfolio engine, and at the bar's open for signals. For paper to keep matching live (P5.5's drift gate), the simulated broker should model a marketable limit order, with a fill only if the bar trades through the limit price. A stop should model a stop-limit, which can fail to fill on a gap. This should be built and its effect measured on the backtests before any live order code.

**Risk checks.** NSE lists 16 compulsory pre-trade checks for algo orders (NSE/INVG/69255, para 11.1). The documents disagree about who owns them:
- For an algo "developed by tech-savvy client – The entire responsibility of the logic of the strategy and RMS requirement as per SEBI/NSE circular will be ensured by the Tech Savvy client" (para 13.1.3).
- The April 2026 consolidated circular says "the responsibility of outcome (Profit/Loss) is with the Tech-savvy client … and the RMS is the responsibility of the Trading Member" (NSE/INVG/73992).

Upstox runs its own checks regardless. Trade Terminal should implement those that apply to it, as P5.2:

| NSE check (para 11.1) | In Trade Terminal |
|---|---|
| 1 Price check (price bands) | Reject a limit price outside the day's band; fetch bands before the open. |
| 2 Quantity check (freeze limits) | Slice above the exchange freeze quantity in our own code; the limits come from the instrument master (to verify, as the plan says). |
| 3 Order value check | Maximum notional per order (already in P5.2). |
| 4–5 Trade and market price protection | No market orders; a limit price within a set percentage of the last price (the "fat-finger" band in P5.2). |
| 6 Cumulative open order value | Total value of unfilled orders capped per account. |
| 7 Automated execution check | "Algo shall account for all executed, unexecuted, and unconfirmed orders … before releasing further orders" and stop automatically on "a loop or a runaway situation". This is the reconciliation in P5.1 plus a runaway stop: halt on repeated rejects, on the rate limiter tripping, or on order count far above plan. |
| 8, 11–15 Margin, position, trading, exposure, turnover and per-security limits | Account kill switch on mark-to-market (P5.2), maximum positions, per-instrument caps. |
| 10 Market-wide position limit | Don't open new F&O positions in stocks in the F&O ban period; check the daily ban list. |
| 9, 16 | Apply to foreign investors and commodities; not applicable. |

## 7. Login and sessions

- **The rules:** OAuth only, with two-factor authentication (SEBI para 5.I.d; NSE/INVG/67858, I.c–d). "All API sessions shall be compulsorily logged out every day before the start of the next trading day" (NSE/INVG/67858, A.8).
- **Upstox tokens:** a token is valid "until 3:30 AM the following day, regardless of the time it was generated". The place-order API is open from 05:30 to 00:00 IST.
- **TOTP login:** Upstox offers one (`/v2/totp-login/authorization/token`) that could log in unattended. That would mean storing the second factor next to the PIN on the same machine, which defeats the point of two-factor authentication.

**Phase 5 needs:** P5.7 as planned, a morning reminder and a one-click login that you complete. Fail closed: no valid token by the open means no orders, and an alert. Don't automate the TOTP.

## 8. Records

Brokers keep an audit trail of API orders and trades for at least five years (NSE/INVG/69255, 10.3). Nothing requires the same of you. Trade Terminal's journal already records every order intent and fill; keep journals for five years so your records can be checked against the broker's.

## 9. For the risk disclosure (P5.8)

SEBI's study of individual traders in equity derivatives (August 2026) found that in FY26:
- **87.7%** of individual traders made a net loss (90.9% in FY25).
- The average loss among loss-makers was **₹1.47 lakh**, against an average profit of ₹1.22 lakh among those who made money.
- Options accounted for **92%** of individual losses.

These figures belong in the confirmation screen before the first live order.

## 10. Questions to settle before go-live

**For Upstox (developer support or the community forum), in writing:**
1. Do I, as a tech-savvy client under 10 orders per second, need to declare or register anything with Upstox before trading through the API? Is my app's key automatically the "predefined API key" for unregistered algos?
2. For the 10 per second and 500 per minute limits, does each auto-sliced child order count, and do modify and cancel calls count against the exchange threshold or only Upstox's limit?
3. May the registered static IP belong to a VPS, or be the exit of a VPN tunnel, rather than a home connection? Must it be an Indian IP?
4. Is a MARKET order with market protection treated as a limit order for the exchange's algo market-order ban? (We plan to send limit orders anyway; the answer decides whether "Exit All Positions" is safe to use in an emergency.)

**For you:**
5. Will anyone other than you, or your spouse, dependent children and dependent parents, ever trade through Trade Terminal? If yes, stop and take advice first (section 2).
6. Which static IP route (section 5)?
7. Do you want a short consultation with a securities lawyer before going live? It isn't required by anything above, but this check is my reading, and the AI-drafted-strategy point (section 2) has no precedent in the documents.

## Sources

Primary documents, read in full:

- SEBI, Safer participation of retail investors in Algorithmic trading, 4 Feb 2025: https://www.sebi.gov.in/legal/circulars/feb-2025/safer-participation-of-retail-investors-in-algorithmic-trading_91614.html (PDF: https://www.sebi.gov.in/sebi_data/attachdocs/feb-2025/1738665456458.pdf)
- SEBI, Extension of timeline (glide path), 30 Sep 2025: https://www.sebi.gov.in/legal/circulars/sep-2025/extension-of-timeline-for-implementation-of-sebi-circular-dated-february-04-2025-on-safer-participation-of-retail-investors-in-algorithmic-trading-_96979.html
- SEBI, listing of algorithmic trading circulars: https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListingAll=yes&search=Algorithmic
- NSE/INVG/67858, implementation standards, 5 May 2025: https://nsearchives.nseindia.com/content/circulars/INVG67858.pdf
- NSE/INVG/69255, detailed operational modalities, 22 Jul 2025: https://nsearchives.nseindia.com/content/circulars/INVG69255.zip
- NSE/INVG/69289, corrigendum, 24 Jul 2025: https://nsearchives.nseindia.com/content/circulars/INVG69289.pdf
- NSE/FAOP/69296, algo market order pre-emptive cancellation, 24 Jul 2025: https://nsearchives.nseindia.com/content/circulars/FAOP69296.pdf
- NSE/INVG/70309, corrigendum, 19 Sep 2025: https://nsearchives.nseindia.com/content/circulars/INVG70309.zip
- NSE FAQ on retail algo, 3 Nov 2025: https://nsearchives.nseindia.com/web/sites/default/files/inline-files/FAQ_Retail%20Algo_03112025_NSE.pdf
- NSE/INVG/73992, consolidated NNF circular, 30 Apr 2026: https://nsearchives.nseindia.com/content/circulars/INVG73992.zip
- Upstox, algo trading circular: https://upstox.com/developer/api-documentation/announcements/algo-trading-circular/
- Upstox, My Apps support for the circular: https://upstox.com/developer/api-documentation/appendix/my-apps-support-for-algo-trading-circular/
- Upstox, static IP APIs: https://upstox.com/developer/api-documentation/announcements/static-ip-apis/
- Upstox, rate limiting: https://upstox.com/developer/api-documentation/rate-limiting/
- Upstox, market price protection: https://upstox.com/developer/api-documentation/announcements/market-protection/
- Upstox, Place Order V3: https://upstox.com/developer/api-documentation/v3/place-order/
- Upstox, Get Token: https://upstox.com/developer/api-documentation/get-token/
- SEBI, Profitability of individual traders in the equity derivatives segment (FY25–FY26), Aug 2026: https://www.sebi.gov.in/sebi_data/attachdocs/aug-2026/1787233506209.pdf

Not read: NSE/CMTR/68802 (cash-segment algo market order rejection), cited here as NSE/FAOP/69296 cites it.
