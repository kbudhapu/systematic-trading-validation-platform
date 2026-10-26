# Case Study: What a Single-Venue Feed Hides (IEX vs SIP)

## Question

Free market-data feeds often carry trades from a single exchange (IEX) instead of the consolidated
tape (SIP), which combines every US venue. For research on liquid ETFs, how much does that
matter?

## Data

- **Files:** two sets of 15-minute bars for SPY and QQQ in the research data store.
  - One set was fetched before the platform switched to SIP. Its feed is not recorded in the data; it
    is **inferred from the file name** as IEX.
  - The other set is explicitly SIP.
- **Window:** the comparison uses the **1,007** calendar days present in both sets (2022-06-21 to
  2026-06-26), restricted to regular trading hours (09:30-16:00 New York time).

## Method

- Count bars and sum volume per symbol in each feed, over identical days and hours.
- No strategy is involved.
- The computation is a short script in the private repository, and its output is stored alongside the
  research artifacts.

## Result

| Symbol | Bars missing in IEX | Volume missing in IEX |
|---|---|---|
| QQQ | 0.43% (26,069 vs 26,181) | **98.49%** |
| SPY | 0.44% (26,066 vs 26,182) | **98.03%** |

The single-venue feed has almost every bar but only about **1.5-2% of the consolidated volume**. The
bars look complete; the volume is not.

## Decision

The pre-market scanner, the platform's live data collector, moved to SIP data on **2026-07-13**. Its
logged candidates are IEX through 2026-07-10 and SIP from 2026-07-13 onward. In total (2026-07-06 to
2026-10-21) it logged 2,052 candidates across 834 US equities over 77 sessions, 1,985 of them on SIP
data.

## What it taught

- A feed can pass a completeness check on bar counts while being badly incomplete on volume. Any signal
  that uses volume, relative volume or liquidity filters would be computed on about 2% of the real
  tape.
- Feed provenance belongs in the data. Here it had to be inferred from file names, which is why the
  scanner now records the feed on every candidate row.
- Check the cheap thing first: this comparison needs no model and settles the question.

*Sources: a private comparison script and its stored output; the scanner's candidate table in the
production research vault.*
