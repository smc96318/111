"""
SMC Live Monitor V9.1 Ultimate Edition
全中文推送 | UTC+8时区 | HTML美化 | Killzone识别
"""
import pandas as pd
import numpy as np
import ccxt
import schedule
import time
import requests
from datetime import datetime, timedelta
import os

# ==========================================
# 配置区
# ==========================================

# Telegram 配置 (建议使用 .env 文件)
TG_TOKEN = os.getenv("TG_TOKEN", "YOUR_BOT_TOKEN_HERE")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "YOUR_CHAT_ID_HERE")
TELEGRAM_ENABLED = True

# 交易参数
SYMBOL = "ETH/USDT"
TIMEFRAME = "15m"
LOOKBACK_BARS = 300

# 策略参数 (与回测完全一致)
INITIAL_CAPITAL = 10000
RISK_PER_TRADE = 0.01
TARGET_RR = 2.0
BE_TRIGGER_RR = 1.0
ATR_MULTIPLIER = 1.0
SL_PADDING_ATR = 0.5

# Killzone 定义 (UTC小时)
KZ_LONDON = [7, 8, 9, 10]
KZ_NY = [12, 13, 14, 15]

# ==========================================
# Telegram 推送 (HTML美化)
# ==========================================

def send_telegram(message):
    """发送 Telegram HTML 格式消息"""
    if not TELEGRAM_ENABLED:
        print("[Telegram] 已禁用")
        return

    if TG_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("[Telegram] 未配置 TOKEN，跳过推送")
        return

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = {
        "chat_id": TG_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }

    for attempt in range(3):
        try:
            response = requests.post(url, data=data, timeout=10)
            if response.status_code == 200:
                print("[Telegram] 推送成功")
                return
            else:
                print(f"[Telegram] 推送失败: {response.status_code}，第 {attempt + 1} 次尝试")
        except Exception as e:
            print(f"[Telegram] 推送异常: {e}，第 {attempt + 1} 次尝试")

        # 重试前的退避等待
        time.sleep(5 * (attempt + 1))

# ==========================================
# 时区工具
# ==========================================

def get_utc8_str(utc_dt):
    """UTC 转 UTC+8 字符串"""
    utc8 = utc_dt + timedelta(hours=8)
    return utc8.strftime("%Y-%m-%d %H:%M:%S")

def get_session_name(utc_hour):
    """获取 Killzone 名称"""
    if utc_hour in KZ_LONDON:
        return "🇬🇧 伦敦开盘"
    elif utc_hour in KZ_NY:
        return "🇺🇸 纽约开盘"
    else:
        return "非 Killzone"

# ==========================================
# 指标计算 (与回测完全一致)
# ==========================================

def calculate_indicators(df):
    """计算 SMA200 + ATR"""
    # SMA 200 (注意: 回测用的是 rolling.mean(), 即 SMA)
    df['sma200'] = df['close'].rolling(200).mean()

    # ATR
    tr = np.maximum(df['high'] - df['low'], np.abs(df['high'] - df['close'].shift(1)))
    df['atr'] = tr.rolling(14).mean()

    # Body Size
    df['body_size'] = abs(df['close'] - df['open'])

    return df

# ==========================================
# 信号检测 (Killzone 时段)
# ==========================================

def check_structure(df):
    """
    检测大K线 FVG (仅在 Killzone 时段)
    返回信号列表
    """
    signals = []

    highs = df['high'].values
    lows = df['low'].values
    closes = df['close'].values
    sma200 = df['sma200'].values
    body_size = df['body_size'].values
    atr = df['atr'].values

    for i in range(2, len(df) - 5):
        if pd.isna(sma200[i]) or pd.isna(atr[i]):
            continue

        current_time = df.index[i]
        hour = current_time.hour

        # 只在 Killzone 时段内识别信号
        if hour not in KZ_LONDON and hour not in KZ_NY:
            continue

        # 大K线判定
        is_large_candle = body_size[i] > (ATR_MULTIPLIER * atr[i])
        if not is_large_candle:
            continue

        # 多头 FVG
        if closes[i] > sma200[i]:
            if lows[i] > highs[i-2]:
                sl_price = highs[i-2] - (SL_PADDING_ATR * atr[i])
                entry_price = lows[i]
                risk = abs(entry_price - sl_price)
                tp_price = entry_price + (risk * TARGET_RR)

                signals.append({
                    'time': current_time,
                    'type': 'LONG',
                    'entry': entry_price,
                    'sl': sl_price,
                    'tp': tp_price,
                    'risk': risk,
                    'atr': atr[i]
                })

        # 空头 FVG
        elif closes[i] < sma200[i]:
            if highs[i] < lows[i-2]:
                sl_price = lows[i-2] + (SL_PADDING_ATR * atr[i])
                entry_price = highs[i]
                risk = abs(entry_price - sl_price)
                tp_price = entry_price - (risk * TARGET_RR)

                signals.append({
                    'time': current_time,
                    'type': 'SHORT',
                    'entry': entry_price,
                    'sl': sl_price,
                    'tp': tp_price,
                    'risk': risk,
                    'atr': atr[i]
                })

    return signals

# ==========================================
# 数据获取
# ==========================================

def fetch_ohlcv(symbol, timeframe, limit=300):
    """从 Binance 获取 K线数据（带重试）"""
    exchange = ccxt.binance({
        'enableRateLimit': True,
        'options': {'defaultType': 'future'}
    })

    for attempt in range(3):
        try:
            bars = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            return df
        except Exception as e:
            print(f"[错误] 获取数据失败: {e}，第 {attempt + 1} 次尝试")
            time.sleep(5 * (attempt + 1))

    print("[错误] 获取数据失败，已超出重试次数")
    return None

# ==========================================
# 主扫描任务
# ==========================================

def job():
    """执行信号扫描 (每15分钟)"""
    now = datetime.utcnow()
    print(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S')}] 开始扫描...")

    try:
        # 获取数据
        df = fetch_ohlcv(SYMBOL, TIMEFRAME, LOOKBACK_BARS)
        if df is None or len(df) < 200:
            print("[跳过] 数据不足")
            return

        # 计算指标
        df = calculate_indicators(df)

        # 检测信号
        signals = check_structure(df)

        if signals:
            print(f"[发现] 检测到 {len(signals)} 个信号!")

            for sig in signals:
                # 格式化信号信息
                direction_emoji = "🟢 做多" if sig['type'] == 'LONG' else "🔴 做空"
                trend_text = "多头趋势" if sig['type'] == 'LONG' else "空头趋势"
                session = get_session_name(sig['time'].hour)

                message = f"""
<b>🎯 SMC V9.1 实盘信号</b>

{direction_emoji}
<b>方向:</b> {trend_text}
<b>时段:</b> {session}

<b>📊 品种:</b> ETH/USDT
<b>⏰ 时间:</b> {get_utc8_str(sig['time'])} [UTC+8]

<b>💰 入场:</b> ${sig['entry']:.2f}
<b>🛑 止损:</b> ${sig['sl']:.2f}
<b>🎯 止盈:</b> ${sig['tp']:.2f}

<b>📏 风险:</b> ${sig['risk']:.2f}
<b>📈 ATR:</b> {sig['atr']:.2f}

<b>盈亏比:</b> 1:2
<b>仓位:</b> 1% 资金

---
<i>由 SMC Live Monitor 自动生成</i>
                """.strip()

                print(f"\n[信号]\n{message}")
                send_telegram(message)
        else:
            hour = now.hour
            in_kz = hour in KZ_LONDON or hour in KZ_NY
            kz_status = "Killzone内" if in_kz else "Killzone外"
            print(f"[无信号] {kz_status}，市场平静")

    except Exception as e:
        print(f"[异常] 扫描任务出错: {e}")
        import traceback
        traceback.print_exc()

# ==========================================
# 心跳检测
# ==========================================

def heartbeat():
    """每小时推送一次系统状态"""
    now = datetime.utcnow()
    msg = f"""
<b>💓 SMC 系统心跳</b>

<b>⏰ 时间:</b> {get_utc8_str(now)} [UTC+8]
<b>📊 品种:</b> {SYMBOL}
<b>📈 周期:</b> {TIMEFRAME}

<i>系统正常运行中...</i>
    """.strip()
    send_telegram(msg)

# ==========================================
# 调度器
# ==========================================

def schedule_jobs():
    """设置定时任务"""
    # 每小时的 :00:05, :15:05, :30:05, :45:05 执行
    schedule.every().hour.at(":00:05").do(job)
    schedule.every().hour.at(":15:05").do(job)
    schedule.every().hour.at(":30:05").do(job)
    schedule.every().hour.at(":45:05").do(job)

    # 每小时 :00:00 发送心跳
    schedule.every().hour.at(":00:00").do(heartbeat)

# ==========================================
# 主程序
# ==========================================

def main():
    print("=" * 60)
    print(" SMC LIVE MONITOR V9.1 - ULTIMATE EDITION")
    print("=" * 60)
    print(f"[配置] 品种: {SYMBOL}")
    print(f"[配置] 周期: {TIMEFRAME}")
    print(f"[配置] Killzone: London 07:00-10:00, NY 12:00-15:00 UTC")
    print(f"[配置] 指标: SMA200 + ATR14 + Body>1.0ATR")
    print(f"[配置] 盈亏比: {TARGET_RR}R")
    print(f"[配置] 时区: UTC+8 显示")
    print(f"[Telegram] 推送: {'启用' if TELEGRAM_ENABLED else '禁用'}")
    print("=" * 60)

    # 启动时发送通知
    start_msg = """
<b>🚀 SMC Live Monitor 已启动</b>

<b>版本:</b> V9.1 Ultimate Edition
<b>策略:</b> Killzone 大K线 FVG
<b>时间:</b> {}

<i>开始监控市场...</i>
    """.format(get_utc8_str(datetime.utcnow())).strip()
    send_telegram(start_msg)

    # 立即执行一次扫描
    job()

    # 设置定时任务
    schedule_jobs()

    print("\n[系统] 调度器已启动，等待下一根K线...")

    # 主循环
    while True:
        try:
            schedule.run_pending()
            time.sleep(10)
        except KeyboardInterrupt:
            print("\n\n[系统] 用户中断，程序退出")

            # 发送停止通知
            stop_msg = f"""
<b>⏹ SMC Live Monitor 已停止</b>

<b>时间:</b> {get_utc8_str(datetime.utcnow())}

<i>系统安全关闭</i>
            """.strip()
            send_telegram(stop_msg)

            break
        except Exception as e:
            print(f"[错误] 主循环异常: {e}")
            time.sleep(30)

if __name__ == "__main__":
    main()
