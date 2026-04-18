"""
V3 Upgrade Verification Tests
Run inside Docker: docker exec -it trading-bot python -m pytest tests/test_v3_verification.py -v
Or standalone:     docker exec -it trading-bot python tests/test_v3_verification.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ═══════════════════════════════════════════════════════════════════════
# TEST 1: All V3 Modules Import Clean
# ═══════════════════════════════════════════════════════════════════════

def test_imports():
    print("\n═══ TEST 1: Module Imports ═══")
    modules = []

    from app.modules.risk_engine import RiskEngine, TradeParameters
    modules.append("risk_engine")

    from app.modules.daily_guard import DailyGuard, daily_guard, AccountDayState
    modules.append("daily_guard")

    from app.modules.executor import BinanceExecutor, OrderResult, PreEntryCheck, PrecisionInfo
    modules.append("executor")

    from app.modules.scanner import MarketScanner, CoinCandidate, BLACKLISTED_PAIRS
    modules.append("scanner")

    from app.modules.ai_engine import ScalpingEngine, AIDecision
    modules.append("ai_engine")

    from app.modules.telegram import TelegramNotifier
    modules.append("telegram")

    from app.config import settings
    modules.append("config")

    from app.utils.state import StateManager, state_manager
    modules.append("state")

    print(f"  ✅ All {len(modules)} modules imported: {', '.join(modules)}")
    return True


# ═══════════════════════════════════════════════════════════════════════
# TEST 2: Risk Engine — V3 Leverage Tiers
# ═══════════════════════════════════════════════════════════════════════

def test_risk_engine_leverage():
    print("\n═══ TEST 2: Risk Engine — Leverage Tiers ═══")
    from app.modules.risk_engine import RiskEngine

    re = RiskEngine()
    tests = [
        # (confidence, is_elite, expected_leverage)
        (65, False, 0),    # Below 70 → NO TRADE
        (70, False, 5),    # 70-80 → 5x
        (75, False, 5),
        (80, False, 5),
        (81, False, 6),    # 81-90 → 6x
        (85, False, 6),
        (90, False, 6),
        (91, False, 8),    # 91+ → 8x
        (95, False, 8),    # 95 but NOT elite → 8x
        (95, True, 10),    # 95+ elite → 10x
        (98, True, 10),
    ]

    passed = 0
    for conf, elite, expected in tests:
        result = re.get_leverage(conf, elite)
        status = "✅" if result == expected else "❌"
        if result != expected:
            print(f"  {status} conf={conf} elite={elite}: expected {expected}x, got {result}x")
        else:
            passed += 1

    print(f"  ✅ {passed}/{len(tests)} leverage tier tests passed")
    return passed == len(tests)


# ═══════════════════════════════════════════════════════════════════════
# TEST 3: Risk Engine — V3 TP/SL Tiers
# ═══════════════════════════════════════════════════════════════════════

def test_risk_engine_tp_sl():
    print("\n═══ TEST 3: Risk Engine — TP/SL Tiers ═══")
    from app.modules.risk_engine import RiskEngine

    re = RiskEngine()
    tests = [
        # (confidence, atr_pct, is_elite, expected_tp, expected_sl)
        (70, 0.0, False, 0.05, 0.02),    # 70-80 → 5%/2%
        (80, 0.0, False, 0.05, 0.02),
        (81, 0.0, False, 0.07, 0.03),    # 81-90 → 7%/3%
        (90, 0.0, False, 0.07, 0.03),
        (91, 0.0, False, 0.09, 0.04),    # 91+ → 9%/4%
        (95, True, False, 0.12, 0.04),   # Elite → 12%/4%  (is_elite param, not setup_grade)
    ]

    # Note: get_tp_sl_pct signature: (confidence, atr_pct, is_elite, setup_grade)
    passed = 0
    for conf, atr_pct, is_elite_flag, expected_tp, expected_sl in tests:
        # For the elite test, confidence=95 and is_elite=True
        if is_elite_flag:
            tp, sl = re.get_tp_sl_pct(conf, atr_pct, is_elite=True)
        else:
            tp, sl = re.get_tp_sl_pct(conf, atr_pct)

        tp_ok = abs(tp - expected_tp) < 0.001
        sl_ok = abs(sl - expected_sl) < 0.001

        if not tp_ok or not sl_ok:
            print(f"  ❌ conf={conf} elite={is_elite_flag}: expected TP={expected_tp}/SL={expected_sl}, got TP={tp}/SL={sl}")
        else:
            passed += 1

    print(f"  ✅ {passed}/{len(tests)} TP/SL tier tests passed")
    return passed == len(tests)


# ═══════════════════════════════════════════════════════════════════════
# TEST 4: Risk Engine — Full Calculate
# ═══════════════════════════════════════════════════════════════════════

def test_risk_engine_calculate():
    print("\n═══ TEST 4: Risk Engine — Full Calculate ═══")
    from app.modules.risk_engine import RiskEngine

    re = RiskEngine()

    # Test 1: Low balance, moderate confidence
    params = re.calculate(
        symbol="BTCUSDT", side="BUY", confidence=75,
        entry_price=60000, atr_pct=1.5, account_balance=50.0,
        min_notional=5.0, min_qty=0.001, step_size=0.001,
        quantity_precision=3, price_precision=2,
    )
    assert params.approved, f"Should be approved, got: {params.reject_reason}"
    assert params.leverage == 5, f"Expected 5x, got {params.leverage}x"
    assert params.setup_grade == "C", f"Expected grade C, got {params.setup_grade}"
    assert params.tp_pct == 5.0, f"Expected TP 5.0%, got {params.tp_pct}%"
    assert params.sl_pct > 0, "SL should be > 0"

    # Test 2: Size multiplier reduces position
    params_full = re.calculate(
        symbol="ETHUSDT", side="SELL", confidence=85,
        entry_price=3000, atr_pct=1.0, account_balance=200.0,
        min_notional=5.0, min_qty=0.001, step_size=0.001,
        quantity_precision=3, price_precision=2,
        size_multiplier=1.0,
    )
    params_reduced = re.calculate(
        symbol="ETHUSDT", side="SELL", confidence=85,
        entry_price=3000, atr_pct=1.0, account_balance=200.0,
        min_notional=5.0, min_qty=0.001, step_size=0.001,
        quantity_precision=3, price_precision=2,
        size_multiplier=0.5,
    )
    assert params_reduced.position_size_usdt < params_full.position_size_usdt, \
        "50% size multiplier should reduce position"

    # Test 3: Below minimum confidence → rejected
    params_low = re.calculate(
        symbol="BTCUSDT", side="BUY", confidence=50,
        entry_price=60000, atr_pct=1.0, account_balance=100.0,
        min_notional=5.0, min_qty=0.001, step_size=0.001,
        quantity_precision=3, price_precision=2,
    )
    assert not params_low.approved, "Should be rejected for low confidence"

    # Test 4: Setup grade A with volume spike
    params_a = re.calculate(
        symbol="BTCUSDT", side="BUY", confidence=95,
        entry_price=60000, atr_pct=1.0, account_balance=500.0,
        min_notional=5.0, min_qty=0.001, step_size=0.001,
        quantity_precision=3, price_precision=2,
        volume_spike=True,
    )
    assert params_a.setup_grade == "A", f"Expected grade A, got {params_a.setup_grade}"
    assert params_a.is_elite, "95+ with A-grade should be elite"

    # Test 5: Position sizing preserved (balance tiers)
    assert re.get_risk_pct(50) == 0.08, "50 balance should be 8% risk"
    assert re.get_risk_pct(200) == 0.06, "200 balance should be 6% risk"
    assert re.get_risk_pct(500) == 0.04, "500 balance should be 4% risk"
    assert re.get_risk_pct(2000) == 0.02, "2000 balance should be 2% risk"

    print(f"  ✅ All full-calculate tests passed")
    return True


# ═══════════════════════════════════════════════════════════════════════
# TEST 5: Daily Guard — Full Lifecycle
# ═══════════════════════════════════════════════════════════════════════

def test_daily_guard():
    print("\n═══ TEST 5: Daily Guard — Full Lifecycle ═══")
    from app.modules.daily_guard import DailyGuard

    guard = DailyGuard()
    acc_id = 999  # Test account

    # Fresh account — should be allowed
    result = guard.check_allowed(acc_id, balance=100.0, confidence=80)
    assert result["allowed"], "Fresh account should be allowed"
    assert result["size_multiplier"] == 1.0, "Fresh account should have full size"

    # Record wins to approach safe mode (+5%)
    guard.record_trade(acc_id, pnl=3.0, balance=100.0)  # +3%
    guard.record_trade(acc_id, pnl=2.5, balance=100.0)  # +5.5% total

    # Should now be in safe mode
    result = guard.check_allowed(acc_id, balance=100.0, confidence=80)
    assert not result["allowed"], "80% confidence should be blocked in safe mode"

    result_elite = guard.check_allowed(acc_id, balance=100.0, confidence=92)
    assert result_elite["allowed"], "92% should be allowed in safe mode"
    assert result_elite["size_multiplier"] == 0.5, "Safe mode should halve size"

    # Test consecutive losses
    guard2 = DailyGuard()
    acc2 = 998
    guard2.record_trade(acc2, pnl=-1.0, balance=200.0)
    guard2.record_trade(acc2, pnl=-1.0, balance=200.0)

    result2 = guard2.check_allowed(acc2, balance=200.0, confidence=85)
    assert result2["allowed"], "2 losses should still allow (with reduction)"
    assert result2["size_multiplier"] == 0.7, f"2 losses should give 0.7 multiplier, got {result2['size_multiplier']}"

    # Test daily PNL retrieval
    pnl_pct = guard.get_daily_pnl_pct(acc_id, balance=100.0)
    assert pnl_pct > 0, "PNL should be positive after wins"

    # Test stats
    stats = guard.get_account_stats(acc_id)
    assert stats["trades_today"] == 2, f"Expected 2 trades, got {stats['trades_today']}"
    assert stats["wins_today"] == 2, f"Expected 2 wins, got {stats['wins_today']}"

    print(f"  ✅ All daily guard tests passed")
    return True


# ═══════════════════════════════════════════════════════════════════════
# TEST 6: Config V3 Values
# ═══════════════════════════════════════════════════════════════════════

def test_config():
    print("\n═══ TEST 6: Config V3 Values ═══")
    from app.config import settings

    checks = [
        ("MAX_SPREAD_PCT", settings.MAX_SPREAD_PCT, 0.15),
        ("DAILY_PROFIT_LIMIT_PCT", settings.DAILY_PROFIT_LIMIT_PCT, 7.0),
        ("DAILY_LOSS_LIMIT_PCT", settings.DAILY_LOSS_LIMIT_PCT, -8.0),
        ("DAILY_SAFE_MODE_PCT", settings.DAILY_SAFE_MODE_PCT, 5.0),
        ("DAILY_LOSS_REDUCE_PCT", settings.DAILY_LOSS_REDUCE_PCT, 5.0),
        ("CONSECUTIVE_LOSS_REDUCE_THRESHOLD", settings.CONSECUTIVE_LOSS_REDUCE_THRESHOLD, 2),
        ("CONSECUTIVE_LOSS_PAUSE_THRESHOLD", settings.CONSECUTIVE_LOSS_PAUSE_THRESHOLD, 4),
        ("CONSECUTIVE_LOSS_PAUSE_MINUTES", settings.CONSECUTIVE_LOSS_PAUSE_MINUTES, 60),
        ("MAX_SPREAD_ENTRY_PCT", settings.MAX_SPREAD_ENTRY_PCT, 0.10),
        ("MIN_CONFIDENCE", settings.MIN_CONFIDENCE, 70),
    ]

    passed = 0
    for name, actual, expected in checks:
        if actual == expected:
            passed += 1
        else:
            print(f"  ❌ {name}: expected {expected}, got {actual}")

    print(f"  ✅ {passed}/{len(checks)} config values correct")
    return passed == len(checks)


# ═══════════════════════════════════════════════════════════════════════
# TEST 7: Scanner — Blacklist + Scoring
# ═══════════════════════════════════════════════════════════════════════

def test_scanner():
    print("\n═══ TEST 7: Scanner — Blacklist + Scoring ═══")
    from app.modules.scanner import MarketScanner, BLACKLISTED_PAIRS, CoinCandidate

    scanner = MarketScanner()

    # Blacklist check
    assert "LUNA2USDT" in BLACKLISTED_PAIRS, "LUNA2USDT should be blacklisted"
    assert "FTTUSDT" in BLACKLISTED_PAIRS, "FTTUSDT should be blacklisted"

    # Excluded set includes blacklist
    assert "LUNA2USDT" in scanner.excluded, "Scanner excluded should contain blacklisted pairs"

    # Multi-factor scoring
    candidate = CoinCandidate(
        symbol="BTCUSDT", price=60000, volume_24h=5_000_000_000,
        price_change_pct=3.5, bid=59990, ask=60000, spread_pct=0.017,
    )
    score = scanner.compute_composite_score(candidate)
    assert score > 0, f"Score should be positive, got {score}"
    assert candidate.volume_score > 0, "Volume score should be set"
    assert candidate.spread_score > 0, "Spread score should be set"
    assert candidate.trend_score > 0, "Trend score should be set"

    # Higher volume should score higher
    candidate2 = CoinCandidate(
        symbol="LOWVOL", price=1.0, volume_24h=10_000_000,
        price_change_pct=3.5, bid=0.99, ask=1.01, spread_pct=2.0,
    )
    score2 = scanner.compute_composite_score(candidate2)
    assert score > score2, f"BTC ({score}) should score higher than low-vol ({score2})"

    print(f"  ✅ All scanner tests passed")
    return True


# ═══════════════════════════════════════════════════════════════════════
# TEST 8: AI Engine — Setup Grade Logic
# ═══════════════════════════════════════════════════════════════════════

def test_ai_engine_grades():
    print("\n═══ TEST 8: AI Engine — Setup Grades ═══")
    from app.modules.ai_engine import ScalpingEngine

    engine = ScalpingEngine()

    assert engine.determine_setup_grade(8, True) == "A", "8 conditions + volume = A"
    assert engine.determine_setup_grade(9, True) == "A", "9 conditions + volume = A"
    assert engine.determine_setup_grade(8, False) == "B", "8 conditions no volume = B"
    assert engine.determine_setup_grade(7, True) == "B", "7 conditions + volume = B"
    assert engine.determine_setup_grade(7, False) == "B", "7 conditions no volume = B"
    assert engine.determine_setup_grade(6, True) == "C", "6 conditions = C"
    assert engine.determine_setup_grade(6, False) == "C", "6 conditions = C"

    # Chase detection
    assert engine.detect_chase(body=0.5, atr=0.2), "Body 0.5 > 2*ATR(0.2) = chase"
    assert not engine.detect_chase(body=0.3, atr=0.2), "Body 0.3 < 2*ATR(0.2) = not chase"

    print(f"  ✅ All AI engine tests passed")
    return True


# ═══════════════════════════════════════════════════════════════════════
# TEST 9: Executor — Data Classes
# ═══════════════════════════════════════════════════════════════════════

def test_executor_classes():
    print("\n═══ TEST 9: Executor — V3 Data Classes ═══")
    from app.modules.executor import BinanceExecutor, OrderResult, PreEntryCheck

    # OrderResult has V3 fields
    result = OrderResult(
        success=True, order_id=12345, symbol="BTCUSDT", side="BUY",
        quantity=0.001, entry_price=60000, fill_price=60001,
        sl_attached=True, tp_attached=False,
    )
    assert result.fill_price == 60001, "V3: fill_price should be set"
    assert result.sl_attached, "SL should be attached"
    assert not result.tp_attached, "TP should not be attached"

    # PreEntryCheck
    check = PreEntryCheck(passed=True, spread_pct=0.05, fee_impact_pct=15.0)
    assert check.passed, "Check should pass"
    assert check.spread_pct == 0.05

    # Fill price extraction
    executor = BinanceExecutor(api_key="test", secret_key="test")
    assert executor._get_fill_price({"avgPrice": "60001.5"}) == 60001.5
    assert executor._get_fill_price({"price": "60000.0"}) == 60000.0
    assert executor._get_fill_price({}) == 0.0

    print(f"  ✅ All executor class tests passed")
    return True


# ═══════════════════════════════════════════════════════════════════════
# TEST 10: Telegram — V3 Methods Exist
# ═══════════════════════════════════════════════════════════════════════

def test_telegram_methods():
    print("\n═══ TEST 10: Telegram — V3 Methods ═══")
    from app.modules.telegram import TelegramNotifier

    notifier = TelegramNotifier()

    v3_methods = [
        "trade_opened",      # Enhanced with tp_pct, sl_pct, grade, daily_pnl
        "tp_sl_failed",      # NEW in V3
        "daily_target_hit",  # NEW in V3
        "daily_loss_hit",    # NEW in V3
        "signal_summary",
        "scan_complete",
        "no_signals",
        "error_alert",
    ]

    for method_name in v3_methods:
        assert hasattr(notifier, method_name), f"Missing method: {method_name}"

    # Check trade_opened accepts V3 params
    import inspect
    sig = inspect.signature(notifier.trade_opened)
    v3_params = ["tp_pct", "sl_pct", "setup_grade", "daily_pnl_pct"]
    for param in v3_params:
        assert param in sig.parameters, f"trade_opened missing V3 param: {param}"

    print(f"  ✅ All {len(v3_methods)} Telegram methods verified, V3 params present")
    return True


# ═══════════════════════════════════════════════════════════════════════
# TEST 11: State Manager — Preserved
# ═══════════════════════════════════════════════════════════════════════

def test_state_manager():
    print("\n═══ TEST 11: State Manager — Preserved ═══")
    from app.utils.state import StateManager

    sm = StateManager()

    # Record trades
    sm.record_trade_opened("BTCUSDT")
    sm.record_trade_opened("ETHUSDT")
    sm.record_trade_closed(5.0)   # Win
    sm.record_trade_closed(-2.0)  # Loss
    sm.record_skip()

    stats = sm.get_stats()
    assert stats["total_trades"] == 2
    assert stats["winning_trades"] == 1
    assert stats["losing_trades"] == 1
    assert stats["skipped_trades_today"] == 1

    # Cooldown check
    on_cd, _ = sm.is_coin_on_cooldown("BTCUSDT")
    assert on_cd, "BTCUSDT should be on cooldown after trading"

    on_cd2, _ = sm.is_coin_on_cooldown("SOLUSDT")
    assert not on_cd2, "SOLUSDT should not be on cooldown"

    print(f"  ✅ All state manager tests passed")
    return True


# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  V3 UPGRADE VERIFICATION SUITE")
    print("=" * 60)

    tests = [
        ("Module Imports", test_imports),
        ("Leverage Tiers", test_risk_engine_leverage),
        ("TP/SL Tiers", test_risk_engine_tp_sl),
        ("Full Calculate", test_risk_engine_calculate),
        ("Daily Guard", test_daily_guard),
        ("Config Values", test_config),
        ("Scanner", test_scanner),
        ("AI Engine Grades", test_ai_engine_grades),
        ("Executor Classes", test_executor_classes),
        ("Telegram Methods", test_telegram_methods),
        ("State Manager", test_state_manager),
    ]

    results = []
    for name, test_fn in tests:
        try:
            passed = test_fn()
            results.append((name, passed))
        except Exception as e:
            print(f"  ❌ EXCEPTION: {e}")
            results.append((name, False))

    print("\n" + "=" * 60)
    print("  RESULTS SUMMARY")
    print("=" * 60)

    total_pass = sum(1 for _, p in results if p)
    total = len(results)

    for name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"  {status}  {name}")

    print(f"\n  {total_pass}/{total} tests passed")

    if total_pass == total:
        print("  🎉 ALL V3 VERIFICATION TESTS PASSED!")
    else:
        print("  ⚠️  Some tests failed — review output above")
        sys.exit(1)
