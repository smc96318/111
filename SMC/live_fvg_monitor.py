"""
SMC V9.1 Live Monitor (Local Risk Management Edition)
Strategy: 15m SMA200 + Momentum FVG (1.0 ATR) + Killzones
Features:
1. [CRITICAL] Fixed London Killzone missing hour (Added 10:00 UTC)
2. [WARNING] Added network retry mechanism for robustness
3. [SEC] Enforced .env configuration
4. [BUGFIX] Added signal deduplication (idempotency) to prevent duplicate pushes
5. [LOCAL] Dynamic position sizing based on LOCAL trade history (5%/3%/2%/1% tiers)
6. [CIRCUIT] Daily loss limit: 3 trades triggers circuit breaker
7. [NO-API] No private API calls - uses local JSON state tracking
"""
import os
import sys
import ccxt
import pandas as pd
import numpy as np
import time
import schedule
import logging
import requests
import json
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone

# 加载环境变量
load_dotenv()

# ================= 🛡️ 安全配置检查 =================
TG_TOKEN = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

if not TG_TOKEN or not TG_CHAT_ID:
    print("[CRITICAL] Telegram config not found!")
    print("Please check .env file for TG_TOKEN and TG_CHAT_ID")
    sys.exit(1)

# ================= ⚙️ 策略参数 (审计锁定) =================
SYMBOL = os.getenv("SYMBOL", "ETH/USDT")
TIMEFRAME = os.getenv("TIMEFRAME", "15m")
LIMIT = 250

# SMC V9.1 硬参数 (与 manual_fvg_v9_1_killzones.py 严格对齐)
ATR_PERIOD = 14
ATR_MULTIPLIER = 1.0     # 动能阈值
SMA_PERIOD = 200         # 趋势线 (SMA)
SL_PADDING = 0.5         # 止损缓冲 (ATR倍数)
RISK_REWARD = 2.0        # 盈亏比

# Killzones (UTC) - [FIXED] 补全回测中的 10:00
KZ_LONDON = [7, 8, 9, 10]  # UTC 07:00 - 10:59 (回测逻辑为 <=10)
KZ_NY = [12, 13, 14, 15]   # UTC 12:00 - 15:59

# 本地状态文件
TRADE_HISTORY_FILE = "trade_history.json"

# ================= 🔧 系统初始化 =================
# 配置 Telegram 会话 (增加重试机制)
tg_session = requests.Session()
retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
tg_session.mount('https://', HTTPAdapter(max_retries=retries))

# 初始化交易所 (仅公开数据，无需 API Key)
exchange = ccxt.binance({
    'enableRateLimit': True,
    'options': {'defaultType': 'future'},
    'timeout': 15000  # 15秒超时
})

# 日志格式
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler("smc_monitor.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# ================= 🔒 信号去重 (幂等性) =================
LAST_SIGNAL_TIME = None  # 记录上次推送的信号时间

# ================= 💰 本地风控追踪系统 =================
class LocalRiskManager:
    """本地状态追踪风控器: 不需要交易所 API"""

    def __init__(self, history_file=TRADE_HISTORY_FILE):
        self.history_file = history_file

    def load_history(self):
        """加载交易历史 JSON"""
        try:
            if os.path.exists(self.history_file):
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return []
        except Exception as e:
            logging.error(f"❌ 读取历史文件失败: {e}")
            return []

    def save_history(self, history):
        """保存交易历史到 JSON"""
        try:
            with open(self.history_file, 'w', encoding='utf-8') as f:
                json.dump(history, f, indent=2, ensure_ascii=False, default=str)
        except Exception as e:
            logging.error(f"❌ 保存历史文件失败: {e}")

    def update_open_trades(self, current_price, current_high, current_low):
        """检查并更新所有 OPEN 状态的交易"""
        history = self.load_history()
        updated = False

        for trade in history:
            if trade['status'] != 'OPEN':
                continue

            entry = trade['entry']
            tp = trade['tp']
            sl = trade['sl']
            direction = trade['type']

            # 判断是否触及止盈或止损
            hit_tp = False
            hit_sl = False

            if 'LONG' in direction:
                if current_high >= tp:
                    hit_tp = True
                elif current_low <= sl:
                    hit_sl = True
            else:  # SHORT
                if current_low <= tp:
                    hit_tp = True
                elif current_high >= sl:
                    hit_sl = True

            if hit_tp:
                trade['status'] = 'CLOSED'
                trade['result'] = 'WIN'
                trade['close_price'] = tp
                trade['close_time'] = datetime.now(timezone.utc).isoformat()
                updated = True
                logging.info(f"✅ 交易止盈: {direction} @ {entry} -> {tp}")
            elif hit_sl:
                trade['status'] = 'CLOSED'
                trade['result'] = 'LOSS'
                trade['close_price'] = sl
                trade['close_time'] = datetime.now(timezone.utc).isoformat()
                updated = True
                logging.info(f"❌ 交易止损: {direction} @ {entry} -> {sl}")

        if updated:
            self.save_history(history)

        return updated

    def calculate_stats(self):
        """计算统计数据: 连亏笔数和今日亏损笔数"""
        history = self.load_history()

        # UTC 今天0点
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

        daily_loss_count = 0
        consecutive_loss_count = 0

        # 统计今日亏损 (遍历所有已关闭交易)
        for trade in history:
            if trade['result'] == 'LOSS':
                try:
                    trade_time = datetime.fromisoformat(trade['time'].replace('Z', '+00:00'))
                    if trade_time >= today_start:
                        daily_loss_count += 1
                except:
                    pass

        # 统计连续亏损 (从最新向旧遍历，遇到 WIN 或 OPEN 停止)
        for trade in reversed(history):
            if trade['result'] == 'LOSS':
                consecutive_loss_count += 1
            elif trade['result'] in ['WIN', 'PENDING']:
                break  # 遇到盈利或未完成，停止计数

        return {
            'daily_loss': daily_loss_count,
            'consecutive_loss': consecutive_loss_count
        }

    def calculate_risk_percent(self):
        """根据战绩动态计算风险比例"""
        stats = self.calculate_stats()

        # 动态风险档位 (基于连续亏损)
        consecutive = stats['consecutive_loss']

        if consecutive >= 10:
            risk_percent = 0.01  # 1% 严防死守
        elif consecutive >= 5:
            risk_percent = 0.02  # 2% 防守模式
        elif consecutive >= 2:
            risk_percent = 0.03  # 3% 谨慎模式
        else:
            risk_percent = 0.05  # 5% 正常模式

        # 熔断机制: 今日亏损 >= 3笔 (覆盖其他档位)
        if stats['daily_loss'] >= 3:
            return 0  # 停止交易

        return risk_percent

    def get_risk_tier_name(self, risk_percent):
        """获取风险档位名称"""
        if risk_percent == 0:
            return "🛑 今日止损触顶"
        elif risk_percent == 0.01:
            return "1% 严防死守"
        elif risk_percent == 0.02:
            return "2% 防守模式"
        elif risk_percent == 0.03:
            return "3% 谨慎模式"
        elif risk_percent == 0.05:
            return "5% 正常模式"
        else:
            return f"{risk_percent*100:.0f}% 未知档位"

    def add_signal(self, signal):
        """添加新信号到历史记录"""
        history = self.load_history()

        # 提取方向类型
        direction = "LONG" if "LONG" in signal['type'] else "SHORT"

        new_trade = {
            'time': signal['time_utc'].isoformat(),
            'type': direction,
            'entry': signal['entry'],
            'sl': signal['sl'],
            'tp': signal['tp'],
            'status': 'OPEN',
            'result': 'PENDING'
        }

        history.append(new_trade)
        self.save_history(history)
        logging.info(f"📝 新信号已记录: {direction} @ {signal['entry']}")

    def is_circuit_breaker(self):
        """检查是否触发熔断"""
        return self.calculate_risk_percent() == 0

    def get_risk_info(self, entry, sl):
        """获取风控信息用于推送"""
        risk_percent = self.calculate_risk_percent()
        stats = self.calculate_stats()

        # 计算止损距离百分比
        sl_distance_pct = abs(entry - sl) / entry * 100

        return {
            'risk_percent': risk_percent,
            'tier_name': self.get_risk_tier_name(risk_percent),
            'consecutive_loss': stats['consecutive_loss'],
            'daily_loss': stats['daily_loss'],
            'sl_distance_pct': sl_distance_pct,
            'is_circuit_breaker': risk_percent == 0
        }

def send_telegram(message):
    """发送精美的 Telegram 消息 (带重试)"""
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        # 使用带重试的 session 发送
        response = tg_session.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            logging.error(f"推送失败: {response.text}")
    except Exception as e:
        logging.error(f"推送出错: {e}")

def fetch_data_with_retry(symbol, timeframe, limit=250, max_retries=3):
    """鲁棒的数据获取函数"""
    for i in range(max_retries):
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            return ohlcv
        except Exception as e:
            logging.warning(f"数据获取失败 ({i+1}/{max_retries}): {e}")
            time.sleep(2)
    logging.error("❌ 数据获取彻底失败，跳过本次扫描")
    return None

def calculate_indicators(df):
    """计算指标 (严格复刻 V9.1)"""
    # 1. 趋势: SMA 200 (审计确认: 回测使用 rolling.mean)
    df['trend'] = df['close'].rolling(SMA_PERIOD).mean()

    # 2. ATR
    df['tr'] = np.maximum(
        df['high'] - df['low'],
        np.abs(df['high'] - df['close'].shift(1))
    )
    df['atr'] = df['tr'].rolling(ATR_PERIOD).mean()

    # 3. Body Size
    df['body_size'] = abs(df['close'] - df['open'])

    return df

def get_utc8_str(utc_dt):
    """将 UTC 时间转换为 北京时间字符串"""
    utc8_dt = utc_dt + timedelta(hours=8)
    return utc8_dt.strftime('%Y-%m-%d %H:%M')

def check_structure(df):
    """分析最新收盘的 K 线"""
    # 审计确认: 实盘必须取 iloc[-2] (刚收盘的完整K线)，iloc[-1] 是跳动中的
    last_closed_idx = -2

    curr = df.iloc[last_closed_idx]      # i (当前判定K线)
    prev = df.iloc[last_closed_idx - 1]  # i-1
    prev2 = df.iloc[last_closed_idx - 2] # i-2

    # 1. 时间过滤 (Killzones) - 使用 UTC 时间判定
    current_hour_utc = curr.name.hour

    session_name = ""
    if current_hour_utc in KZ_LONDON:
        session_name = "🇬🇧 伦敦开盘 (London)"
    elif current_hour_utc in KZ_NY:
        session_name = "🇺🇸 纽约开盘 (NY)"
    else:
        return None # 非核心时间

    # 2. 动能过滤 (Body > 1.0 ATR)
    if curr['body_size'] <= (ATR_MULTIPLIER * curr['atr']):
        return None

    signal = None

    # 3. BULLISH FVG
    if curr['close'] > curr['trend']:
        if curr['low'] > prev2['high']: # FVG 结构
            atr_val = curr['atr']
            entry_price = curr['low']
            sl_price = prev2['high'] - (atr_val * SL_PADDING)

            signal = {
                'type': '🟢 <b>做多 (LONG)</b>',
                'entry': entry_price,
                'sl': sl_price,
                'price': curr['close'],
                'session': session_name,
                'atr': atr_val
            }

    # 4. BEARISH FVG
    elif curr['close'] < curr['trend']:
        if curr['high'] < prev2['low']: # FVG 结构
            atr_val = curr['atr']
            entry_price = curr['high']
            sl_price = prev2['low'] + (atr_val * SL_PADDING)

            signal = {
                'type': '🔴 <b>做空 (SHORT)</b>',
                'entry': entry_price,
                'sl': sl_price,
                'price': curr['close'],
                'session': session_name,
                'atr': atr_val
            }

    if signal:
        risk = abs(signal['entry'] - signal['sl'])
        if "LONG" in signal['type']:
            signal['tp'] = signal['entry'] + (risk * RISK_REWARD)
        else:
            signal['tp'] = signal['entry'] - (risk * RISK_REWARD)
        signal['time_utc'] = curr.name

    return signal

def job():
    """核心任务 (带信号去重 + 本地风控追踪)"""
    global LAST_SIGNAL_TIME

    try:
        logging.info(f"⏳ 正在扫描 {SYMBOL} ...")

        # 使用带重试的获取函数
        ohlcv = fetch_data_with_retry(SYMBOL, TIMEFRAME, limit=LIMIT)
        if ohlcv is None:
            return

        # 数据验证
        if len(ohlcv) < SMA_PERIOD + 10:
            logging.warning(f"⚠️ 数据不足 ({len(ohlcv)} 条)，需要至少 {SMA_PERIOD + 10} 条")
            return

        df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
        df['time'] = pd.to_datetime(df['time'], unit='ms', utc=True)
        df.set_index('time', inplace=True)

        df = calculate_indicators(df)

        # 💰 先更新本地持仓状态 (检查是否有 TP/SL 触发)
        last_candle = df.iloc[-1]
        risk_mgr = LocalRiskManager()
        risk_mgr.update_open_trades(
            current_price=last_candle['close'],
            current_high=last_candle['high'],
            current_low=last_candle['low']
        )

        signal = check_structure(df)

        if signal:
            # 🔒 信号去重检查: 防止重复推送同一根K线的信号
            try:
                signal_time_str = signal['time_utc'].strftime('%Y-%m-%d %H:%M:%S')
            except Exception as e:
                logging.error(f"❌ 时间格式化失败: {e}")
                signal_time_str = "Unknown"

            if LAST_SIGNAL_TIME is not None and signal['time_utc'] == LAST_SIGNAL_TIME:
                logging.info(f"🔄 检测到重复信号 ({signal_time_str})，跳过推送")
                return

            # 💰 获取风控信息
            risk_info = risk_mgr.get_risk_info(signal['entry'], signal['sl'])

            # 熔断机制: 今日止损触顶
            if risk_info['is_circuit_breaker']:
                circuit_msg = (
                    f"🛑 <b>SMC 风控熔断触发</b>\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"📊 <b>标的:</b> {SYMBOL}\n"
                    f"📅 <b>日期:</b> {get_utc8_str(datetime.now(timezone.utc))} (UTC+8)\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"❌ <b>今日亏损笔数: {risk_info['daily_loss']}</b>\n"
                    f"🚫 <b>系统已暂停推送信号</b>\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"<i>请检查今日交易记录，明日自动恢复。</i>"
                )
                logging.warning(f"🛑 熔断触发: 今日亏损 {risk_info['daily_loss']} 笔")
                send_telegram(circuit_msg)
                return

            # 新信号: 推送并更新记录
            try:
                time_cn = get_utc8_str(signal['time_utc'])

                # 判断是否处于防守模式
                is_defensive = risk_info['risk_percent'] < 0.05
                risk_emoji = "⚠️" if is_defensive else "✅"

                msg = (
                    f"🐯 <b>SMC 狙击信号 (V9.1)</b>\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"📊 <b>标的:</b> #{SYMBOL.replace('/','')} ({TIMEFRAME})\n"
                    f"🧭 <b>方向:</b> {signal['type']}\n"
                    f"🕒 <b>时间:</b> {time_cn} (UTC+8)\n"
                    f"🏙️ <b>时段:</b> {signal['session']}\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"🎯 <b>入场:</b> <code>{signal['entry']:.2f}</code>\n"
                    f"🛡️ <b>止损:</b> <code>{signal['sl']:.2f}</code>\n"
                    f"💰 <b>止盈:</b> <code>{signal['tp']:.2f}</code>\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"📉 <b>连亏/日亏:</b> {risk_info['consecutive_loss']} / {risk_info['daily_loss']}\n"
                    f"{risk_emoji} <b>风控建议:</b> {risk_info['tier_name']}\n"
                    f"🛡️ <b>止损距离:</b> {risk_info['sl_distance_pct']:.2f}%\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"🌊 <b>动能:</b> {signal['atr']:.2f} ATR\n"
                    f"<i>⚠️ 机器自动推送，请复核盘面结构。</i>"
                )
                logging.info(f"🔥 发现新信号! {signal['type']} @ {signal_time_str} | 风险: {risk_info['tier_name']}")
                send_telegram(msg)

                # 记录信号到本地历史
                risk_mgr.add_signal(signal)

                # 更新最后推送时间
                LAST_SIGNAL_TIME = signal['time_utc']
            except Exception as e:
                logging.error(f"❌ 信号处理失败: {e}")
                # 即使推送失败，也要更新时间防止重复
                LAST_SIGNAL_TIME = signal['time_utc']
        else:
            logging.info("💤 扫描完成: 无信号")

    except KeyboardInterrupt:
        logging.info("⏹ 用户中断扫描")
        raise
    except Exception as e:
        logging.error(f"❌ 运行未知错误: {e}")
        import traceback
        logging.error(traceback.format_exc())

def heartbeat():
    """发送心跳"""
    try:
        ticker = exchange.fetch_ticker(SYMBOL)
        logging.info(f"[心跳] 系统正常 | 价格: {ticker['last']}")
    except:
        logging.info("[心跳] 系统正常 (行情获取失败)")

# ================= 🏁 启动主程序 =================
if __name__ == "__main__":
    print("="*40)
    print(f" SMC V9.1 Live Monitor (Local Risk) - {SYMBOL}")
    print("="*40)

    start_time = get_utc8_str(datetime.now(timezone.utc))
    send_telegram(f"🚀 <b>SMC V9.1 监控已上线</b>\n📅 启动时间: {start_time} (UTC+8)\n✅ 本地风控模式 (无需 API)")

    job()

    # 定时任务 (K线收盘后5秒)
    schedule.every().hour.at(":00:05").do(job)
    schedule.every().hour.at(":15:05").do(job)
    schedule.every().hour.at(":30:05").do(job)
    schedule.every().hour.at(":45:05").do(job)

    schedule.every(1).hours.do(heartbeat)

    # 主循环 (永不崩溃)
    while True:
        try:
            schedule.run_pending()
            time.sleep(1)
        except KeyboardInterrupt:
            logging.info("⏹ 用户停止程序")
            stop_time = get_utc8_str(datetime.now(timezone.utc))
            send_telegram(f"⏹ <b>SMC V9.1 监控已停止</b>\n📅 停止时间: {stop_time} (UTC+8)")
            break
        except Exception as e:
            logging.error(f"❌ 主循环异常: {e}")
            import traceback
            logging.error(traceback.format_exc())
            # 等待 30 秒后继续，防止快速崩溃循环
            time.sleep(30)
