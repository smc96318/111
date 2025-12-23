"""
SMC Trading Engine V9.1 (Killzones Filtered)
Strategy: V9.0 + London/NY Killzones
Core Concept: "Trade only during Prime Sessions"
"""
import pandas as pd
import numpy as np

# ==========================================
# 核心配置 (V9.1)
# ==========================================
INITIAL_CAPITAL = 10000
RISK_PER_TRADE = 0.01    # 1% 风险
TARGET_RR = 2.0          # 2.0R 目标
BE_TRIGGER_RR = 1.0      # 1.0R 保本
ATR_MULTIPLIER = 1.0     # Body > 1.0 ATR
SL_PADDING_ATR = 0.5     # 止损缓冲
DATA_FILE = "ETH_15m_Binance_Historical.csv"

# Killzones (UTC小时)
KZ_LONDON_START = 7
KZ_LONDON_END = 10
KZ_NY_START = 12
KZ_NY_END = 15

# ==========================================
# 1. 核心算法
# ==========================================

def is_killzone_hour(timestamp):
    """判断是否在 Killzone 时段内"""
    hour = timestamp.hour
    return (KZ_LONDON_START <= hour <= KZ_LONDON_END) or \
           (KZ_NY_START <= hour <= KZ_NY_END)

def calculate_features(df):
    """计算趋势和ATR"""
    # EMA 200 趋势
    df['ema200'] = df['close'].rolling(200).mean()

    # ATR
    tr = np.maximum(df['high'] - df['low'], np.abs(df['high'] - df['close'].shift(1)))
    df['atr'] = tr.rolling(14).mean()

    # Body Size
    df['body_size'] = abs(df['close'] - df['open'])

    return df

def detect_displacement_fvgs(df):
    """
    V9.1: 只在 Killzone 时段检测大K线 FVG
    """
    fvgs = []

    highs = df['high'].values
    lows = df['low'].values
    closes = df['close'].values
    htf_ema = df['ema200'].values
    body_size = df['body_size'].values
    atr = df['atr'].values

    print("[分析] 扫描 V9.1 Killzone FVG...")

    for i in range(2, len(df) - 50):
        if pd.isna(htf_ema[i]) or pd.isna(atr[i]):
            continue

        # ===== 新增: 时间过滤 =====
        # 只在 Killzone 时段内识别 FVG
        if not is_killzone_hour(df.index[i]):
            continue

        # 大K线判定 (Body > 1.0 ATR)
        is_large_candle = body_size[i] > (ATR_MULTIPLIER * atr[i])

        if not is_large_candle:
            continue

        # Bullish FVG (顺势做多)
        if closes[i] > htf_ema[i]:
            if lows[i] > highs[i-2]:
                fvgs.append({
                    'time': df.index[i],
                    'type': 'Bullish',
                    'top': lows[i],
                    'bottom': highs[i-2],
                    'mitigated': False,
                    'created_at': i
                })

        # Bearish FVG (顺势做空)
        elif closes[i] < htf_ema[i]:
            if highs[i] < lows[i-2]:
                fvgs.append({
                    'time': df.index[i],
                    'type': 'Bearish',
                    'top': lows[i-2],
                    'bottom': highs[i],
                    'mitigated': False,
                    'created_at': i
                })

    return fvgs

# ==========================================
# 2. 策略引擎
# ==========================================

def check_signal(i, df, fvgs):
    if i < 200:
        return None

    candle = df.iloc[i]
    atr = candle['atr']

    if pd.isna(atr):
        return None

    for fvg in fvgs:
        if fvg['created_at'] >= i:
            continue
        if fvg['mitigated']:
            continue
        if i - fvg['created_at'] > 200:
            continue

        # Long
        if fvg['type'] == 'Bullish':
            if candle['low'] <= fvg['top']:
                sl_price = fvg['bottom'] - (SL_PADDING_ATR * atr)
                return {
                    'type': 'LONG',
                    'entry': fvg['top'],
                    'sl': sl_price,
                    'fvg': fvg
                }

        # Short
        if fvg['type'] == 'Bearish':
            if candle['high'] >= fvg['bottom']:
                sl_price = fvg['top'] + (SL_PADDING_ATR * atr)
                return {
                    'type': 'SHORT',
                    'entry': fvg['bottom'],
                    'sl': sl_price,
                    'fvg': fvg
                }

    return None

# ==========================================
# 3. 回测循环
# ==========================================

def run_backtest(df):
    print("[系统] 启动 SMC V9.1 引擎 (Killzones)...")

    print("[预处理] 计算特征...")
    df = calculate_features(df)

    fvgs = detect_displacement_fvgs(df)
    print(f"[数据] 筛选出 {len(fvgs)} 个 Killzone 动能 FVG")

    trades = []
    capital = INITIAL_CAPITAL
    i = 200

    while i < len(df) - 1:
        signal = check_signal(i, df, fvgs)

        if signal:
            entry = signal['entry']
            sl = signal['sl']
            direction = signal['type']

            risk = abs(entry - sl)
            if risk == 0:
                i += 1
                continue

            risk_amt = capital * RISK_PER_TRADE

            # 2.0R 目标
            tp = entry + (risk * TARGET_RR) if direction == 'LONG' else entry - (risk * TARGET_RR)
            be_trigger = entry + (risk * BE_TRIGGER_RR) if direction == 'LONG' else entry - (risk * BE_TRIGGER_RR)

            outcome = 'Running'
            pnl = 0
            signal['fvg']['mitigated'] = True
            is_be = False

            # 模拟持仓 (持仓不受时间限制)
            for j in range(i + 1, len(df)):
                future = df.iloc[j]

                if direction == 'LONG':
                    current_sl = entry if is_be else sl
                    if future['low'] <= current_sl:
                        outcome = 'BE' if is_be else 'Loss'
                        pnl = 0 if is_be else -risk_amt
                        break
                    if future['high'] >= tp:
                        outcome = 'Win'
                        pnl = risk_amt * TARGET_RR
                        break
                    if not is_be and future['high'] >= be_trigger:
                        is_be = True

                else:  # SHORT
                    current_sl = entry if is_be else sl
                    if future['high'] >= current_sl:
                        outcome = 'BE' if is_be else 'Loss'
                        pnl = 0 if is_be else -risk_amt
                        break
                    if future['low'] <= tp:
                        outcome = 'Win'
                        pnl = risk_amt * TARGET_RR
                        break
                    if not is_be and future['low'] <= be_trigger:
                        is_be = True

            if outcome != 'Running':
                capital += pnl
                trades.append({
                    'Time': df.index[i],
                    'Type': direction,
                    'Result': outcome,
                    'PnL': pnl,
                    'Balance': capital
                })
                i = j

        i += 1

    return pd.DataFrame(trades), capital

# ==========================================
# 4. 主程序
# ==========================================

def main():
    print("=" * 60)
    print(" SMC SYSTEM V9.1 - KILLZONES FILTERED")
    print("=" * 60)

    try:
        df = pd.read_csv(DATA_FILE)
        df.columns = [c.lower() for c in df.columns]

        # 处理时间戳
        if 'open_time' in df.columns:
            df['timestamp'] = pd.to_datetime(df['open_time'], unit='ms')
        elif 'timestamp' in df.columns:
            df['timestamp'] = pd.to_datetime(df['timestamp'])

        df.set_index('timestamp', inplace=True)

        print(f"\n[数据] 加载 {DATA_FILE}")
        print(f"[数据] K线数量: {len(df)}")
        print(f"[数据] 时间范围: {df.index[0]} 到 {df.index[-1]}")
        print(f"[Killzone] London: 07:00-10:00 UTC")
        print(f"[Killzone] New York: 12:00-15:00 UTC")

        trades, final_cap = run_backtest(df)

        print("\n" + "=" * 60)
        print(f"最终余额: ${final_cap:,.2f}")
        print(f"ROI: {(final_cap - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100:.2f}%")

        if not trades.empty:
            print(f"\n总交易: {len(trades)}")
            wins = len(trades[trades['Result'] == 'Win'])
            be_count = len(trades[trades['Result'] == 'BE'])
            losses = len(trades[trades['Result'] == 'Loss'])
            win_rate = wins / len(trades) * 100
            print(f"盈利: {wins}")
            print(f"保本: {be_count}")
            print(f"亏损: {losses}")
            print(f"胜率: {win_rate:.2f}%")
        else:
            print("\n无交易记录")

    except FileNotFoundError:
        print(f"[错误] 找不到文件: {DATA_FILE}")
    except Exception as e:
        print(f"[错误] {e}")

if __name__ == "__main__":
    main()
