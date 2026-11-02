# Office candlestick wall

Authenticated Alpha order; Epsilon UI, Beta OHLC API, Zeta review, Alpha publication. Source base e30b4e62d. No API/provider/runtime/execution changes.

Reuse unchanged vendored TradingView Lightweight Charts4.1.1 with recorded hash. Consume existing candles.fields and5s/1m arrays: t,o,h,l,c,buy_sol,sell_sol,buy_n,sell_n. USD candles and SOL volume are distinct. OHLC crosshair readout comes from the exact hovered candle. Missing buckets add whitespace, never synthetic bars; future, unsorted or malformed buckets show an explicit unavailable/empty reason. Unknown volume is a lower bound; zero is a value. Intervals and manual time zoom survive refresh.

Entry/exit markers use stored modelled historical USD prices and recorded times, not current FX. Approximate stop/TP unit-price references derive from the stored entry and host percentage levels; they are not executable quote proof. Default scale fits candles and fill markers. Show stop/TP fits distant references; Fit candles returns to the useful price view. Disabled/missing rails are stated. Existing candle FX/basis stays in source details, separate from historical fill prices.

Keep chart wall and expansion dialogs inside the rendered room. Small multiples stay bounded; overflow expands or scrolls within the wall. Phone uses one readable chart column. All price formatting supports sub-cent values. No line-chart fallback when OHLC is absent. An invisible reference series supports real markers and price lines only.

Validation: actual vendored Chromium candle/histogram series, real mouse OHLC, both intervals, whitespace gaps, malformed/future data, zero/partial volume, readable price formatting, visible fitted rails, manual follow/zoom, vendor hash, room/phone containment.145 focused tests passed before final scale control; affected checks rerun after it. Real served snapshot proof in ~/.cache/epsilon-office-candles-proof/ includes2 positions,BEAST64/6 andDAVID135/13 bars at5s/1m, and desktop/phone room+expanded+crosshair screenshots. Candidate assets/API enrichment only, not publication. Rollback is Alpha's prior web release.
