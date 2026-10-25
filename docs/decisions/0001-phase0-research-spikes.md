# 0001: Phase 0 research spikes (P0.10)

Date: 2026-10-25. Status: **open; all three need a first-party answer.**

Desk research from public sources. None of the three questions could be settled from what's published. Each section gives what was found, what it means for the plan, and the one action that would settle it.

This is engineering research, not legal advice.

---

## 1. May market data from the Upstox API be stored locally, and for how long?

**Gates:** the *Keep on this machine* option at quit (plan §4.6).

**Found:**
- Upstox's public API documentation pages don't state storage or retention terms. The binding terms are the ones accepted with the "I accept the terms and conditions" checkbox when creating a developer app, and those weren't found publicly.
- Informal answers from Upstox staff on the developer community forum say that analysis and order placement for your own use are permitted, and that sharing *derived* analytics (signals, reports) needs no extra approval, with rate limits as the main condition ([API usage clarification](https://community.upstox.com/t/api-usage-clarification/13250), [personal website use](https://community.upstox.com/t/usage-of-api-for-personally-owned-website/4715)). Forum answers aren't the agreement.
- Exchange data policies (NSE, BSE, MCX) separately govern redistribution of exchange data. Trade Terminal never redistributes: each user downloads with their own token onto their own machine.

**Implication:** nothing found forbids local storage for the account holder's own use, but nothing confirms it either. The session cache (discard at quit) is safe in every reading and proceeds as planned. *Keep* is built behind a setting and stays off by default until this is answered.

**Action (owner: you):** read the terms text on the Upstox developer app page, and email Upstox API support asking: "May an account holder store historical candles fetched through the API on their own machine indefinitely, for their own backtesting, with no redistribution?" Record the answer here.

---

## 2. Is Upstox Plus worth it for real option premiums?

**Gates:** P4.3 (real option premiums), and how option data is partitioned on disk.

**Found:**
- The Expired Instruments APIs (expiries, expired option and future contracts, expired historical candles) exist ([docs](https://upstox.com/developer/api-documentation/expired-instruments/), [launch announcement](https://upstox.com/developer/api-documentation/announcements/expired-instruments-api/), [candle endpoint](https://upstox.com/developer/api-documentation/get-expired-historical-candle-data/)), and Upstox publishes a [backtesting workflow](https://upstox.com/developer/api-documentation/backtesting/) built on them: list past expiries, list the contracts that settled, fetch their candles.
- Access requires the Upstox Plus plan, according to the launch announcement and a third-party downloader ([ExpiryTrack](https://github.com/marketcalls/ExpiryTrack)), which also reports that Plus activation is currently free "and may become chargeable in future". That is not an official price.
- **History depth is not documented.** A docs example uses a 2022 contract, which suggests multi-year history, and one [community thread](https://community.upstox.com/t/how-to-get-older-symbols-and-historical-options-data/13146) reports missing NIFTY option data for a 2024 expiry.
- **Supported intervals are inconsistent between pages:** one lists 1, 3, 5, 15 and 30 minutes plus daily; another lists 1-minute, 30-minute, daily, weekly and monthly. 1-minute is in both, and 1-minute is all the plan needs (§4.5 derives the rest).

**Implication:** promising, and possibly free today. The planned Parquet layout (partitioned by instrument, base timeframe and month, P1.3) suits per-contract option data without change, so Phase 1 isn't blocked.

**Action (owner: you, about 30 minutes):** check the Plus price and activation in the Upstox app; then, with a Plus token, fetch 1-minute candles for one NIFTY weekly ATM option from each of 2022, 2023, 2024 and 2025 and note which return data. That answers depth and gaps directly. I can write the script once Plus is active.

---

## 3. How does SEBI's retail algo framework apply to Trade Terminal?

**Gates:** what the beta may ship (P4.7, P4.8), and Phase 7 entry.

**Found** (secondary sources: law-firm and broker articles; the primary circular text wasn't retrieved):
- SEBI's circular of 4 February 2025 created the framework for retail API algo trading through brokers, either self-run with the client's own API key or via an "algo provider" ([summary](https://ksandk.com/corporate/sebis-new-regulatory-framework-for-retail-algorithmic-trading/), [another](https://www.indialaw.in/blog/civil/sebi-retail-investors-algorithmic-trading/)).
- Exchange implementation standards set an initial threshold of **10 orders per second per exchange**: client-built algos below it don't need exchange registration ([ICICI Direct](https://www.icicidirect.com/futures-and-options/articles/algorithmic-trading-new-rules-by-sebi-nse-retail-participation-with-safety-and-structure), [NSE standards summary](https://www.finseclaw.com/article/nse-releases-implementation-standards-for-retail-participation-in-algorithmic-trading)). Every API algo order carries an exchange-issued identifier, which the broker handles.
- Third-party algo providers must be empanelled with exchanges; "black box" strategies (logic hidden from the user) additionally need the provider registered as a Research Analyst ([AlgoBulls](https://algobulls.com/blog/industry-insights-and-updates/sebi-new-algotrading-regulations-for-retail-investors-2026), [Liquide](https://blog.liquide.life/sebi-algo-trading-regulations-2026/)).
- Timeline: the original 1 August 2025 start was extended, and full implementation was mandated from **1 April 2026**, with brokers that missed milestones barred from onboarding new API algo clients from 5 January 2026 ([Taxmann](https://www.taxmann.com/post/blog/sebi-extends-deadline-for-retail-algo-trading-framework), [BSE glide path](https://rhnvrm.github.io/stock-market-circulars/circulars/bse/2025/bse-2025-11-24-e6eac9e12bd239db-adherence-to-milestones-and-the-rules-related-to-retail-algo/)).
- Static-IP registration for API order placement is widely reported but wasn't confirmed against a primary source here.
- Sources conflict on one point: some say every strategy must be exchange-approved before going live, while others say only those above 10 OPS need registration.

**Implication:**
- **Phases 0 to 6 place no orders.** Paper trading simulates fills locally and sends nothing to the exchange, so the framework doesn't bite before Phase 7.
- **The open question for the beta is the "algo provider" line.** A trader running their own strategy, written by them (or by an LLM at their instruction) in a tool they installed, with every rule visible in the read-back, looks closest to the self-run, white-box, below-10-OPS case. Shipping ready-made strategies (templates) or later executing orders for others moves toward the provider side. The read-back design (code-generated, every rule visible) is the right side of white-box versus black-box either way.

**Action (owner: you, before P4.8 private beta and again before Phase 7):** get a short written opinion from a SEBI-practice lawyer, or written confirmation from Upstox (as the broker responsible for algo-ID tagging), on: (a) does distributing Trade Terminal, which places orders only with each user's own API key and runs strategies the user defines, make the author an algo provider; (b) do the bundled templates change that; (c) current OPS threshold and static-IP rules for order placement.

---

## Effect on the plan

- No Phase 1 work is blocked.
- *Keep on this machine* ships off by default until question 1 is answered.
- P4.3's design waits on question 2's 30-minute check.
- P4.7 and P4.8 wait on question 3's opinion; Phase 7 already required it.
