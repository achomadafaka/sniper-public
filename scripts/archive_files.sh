#!/bin/bash
set -e

ARCHIVE_FILES=(
  analyze_strategies.py
  analyze_what_works.py
  classification_system.py
  debug_wallet.py
  deep_analysis.py
  deep_wallet_analysis.py
  drawdown_monitor.py
  dynamic_analyzer.py
  entry_freshness.py
  final_strategy.py
  final_strategy_v2.py
  insider_detector.py
  market_allocator.py
  metrics_calculator.py
  monitor.py
  news_fetcher.py
  sniper_discovery.py
  temporal_analyzer.py
  trade_classifier.py
  update_prices.py
  wallet_analyzer.py
  wallet_blocker.py
  wallet_classifier.py
)

for f in "${ARCHIVE_FILES[@]}"; do
  if [[ ! -f "$f" ]]; then
    echo "SKIP: $f (not found)"
    continue
  fi
  
  case "$f" in
    *analysis*|deep_*|temporal_*|dynamic_*)
      tgt="archive/analysis/$f"
      ;;
    *classifier*|insider_detector.py|wallet_*)
      tgt="archive/classifiers/$f"
      ;;
    final_strategy.py|final_strategy_v2.py|sniper_discovery.py|trade_classifier.py)
      tgt="archive/strategy/$f"
      ;;
    drawdown_monitor.py|monitor.py|news_fetcher.py)
      tgt="archive/ops/$f"
      ;;
    *)
      tgt="archive/debug/$f"
      ;;
  esac
  
  echo "git mv $f -> $tgt"
  git mv "$f" "$tgt"
done
