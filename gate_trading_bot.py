# gate_trading_bot.py
import time
import hmac
import hashlib
import requests
from datetime import datetime
import json
import logging
from flask import Flask, render_template, request, jsonify
from threading import Thread
import sqlite3
import os

# 設定日誌
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('trading_bot.log'),
        logging.StreamHandler()
    ]
)

class TelegramNotifier:
    """Telegram 通知功能"""
    def __init__(self, bot_token, chat_id):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = bool(bot_token and chat_id)
    
    def send_message(self, message):
        """發送 Telegram 訊息"""
        if not self.enabled:
            return
        
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            data = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML"
            }
            requests.post(url, data=data)
        except Exception as e:
            logging.error(f"Telegram 發送失敗: {e}")

class DatabaseManager:
    """資料庫管理"""
    def __init__(self, db_name='trading_bot.db'):
        self.db_name = db_name
        self.init_db()
    
    def init_db(self):
        """初始化資料庫"""
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        
        # 交易記錄表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trade_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                symbol TEXT,
                side TEXT,
                position_type TEXT,
                price REAL,
                amount REAL,
                action TEXT,
                profit REAL,
                notes TEXT
            )
        ''')
        
        # 參數配置表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS config_presets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                long_amount REAL,
                short_amount REAL,
                add_threshold REAL,
                take_profit REAL
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def add_trade_record(self, symbol, side, position_type, price, amount, action, profit=0, notes=''):
        """新增交易記錄"""
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO trade_records (timestamp, symbol, side, position_type, price, amount, action, profit, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (datetime.now().isoformat(), symbol, side, position_type, price, amount, action, profit, notes))
        conn.commit()
        conn.close()
    
    def get_trade_records(self, limit=100):
        """獲取交易記錄"""
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM trade_records ORDER BY id DESC LIMIT ?', (limit,))
        records = cursor.fetchall()
        conn.close()
        return records

class GateTradingBot:
    def __init__(self, api_key, api_secret, testnet=True):
        self.api_key = api_key
        self.api_secret = api_secret
        
        if testnet:
            self.base_url = "https://fx-api-testnet.gateio.ws/api/v4"
            logging.info("🧪 使用測試網環境")
        else:
            self.base_url = "https://api.gateio.ws/api/v4"
            logging.info("⚠️ 使用正式網環境")
        
        self.db = DatabaseManager()
        self.telegram = None
        self.running = False
        self.current_config = {}
        self.positions = {}
        self.active_symbols = []
    
    def set_telegram(self, bot_token, chat_id):
        """設定 Telegram 通知"""
        self.telegram = TelegramNotifier(bot_token, chat_id)
        if self.telegram.enabled:
            self.telegram.send_message("🤖 交易機器人已啟動")
    
    def _sign(self, method, url, query_string='', payload_string=''):
        """生成 Gate.io API 簽名"""
        t = str(int(time.time()))
        m = hashlib.sha512()
        m.update(payload_string.encode('utf-8'))
        hashed_payload = m.hexdigest()
        
        s = '%s\n%s\n%s\n%s\n%s' % (method, url, query_string, hashed_payload, t)
        sign = hmac.new(self.api_secret.encode('utf-8'), s.encode('utf-8'), hashlib.sha512).hexdigest()
        
        return {'KEY': self.api_key, 'Timestamp': t, 'SIGN': sign}
    
    def get_futures_contracts(self):
        """獲取所有永續合約交易對"""
        try:
            url = f"{self.base_url}/futures/usdt/contracts"
            response = requests.get(url)
            contracts = response.json()
            
            # 只返回活躍的合約
            active_contracts = [
                {
                    'name': c['name'],
                    'underlying': c.get('underlying', ''),
                    'type': c.get('type', '')
                }
                for c in contracts if c.get('in_delisting') == False
            ]
            
            logging.info(f"獲取到 {len(active_contracts)} 個永續合約")
            return active_contracts
        except Exception as e:
            logging.error(f"獲取合約列表錯誤: {e}")
            return []
    
    def get_current_price(self, symbol):
        """獲取當前價格"""
        try:
            url = f"{self.base_url}/futures/usdt/contracts/{symbol}"
            response = requests.get(url)
            data = response.json()
            
            if data:
                price = float(data.get('last_price', 0))
                return price
            return None
        except Exception as e:
            logging.error(f"獲取價格錯誤: {e}")
            return None
    
    def place_futures_order(self, symbol, size, price=0, tif='ioc', close=False):
        """
        下永續合約單
        
        Args:
            symbol: 合約名稱
            size: 合約數量 (正數=做多, 負數=做空)
            price: 價格 (0=市價單)
            tif: 時效性
            close: 是否為平倉單
        """
        try:
            url = "/futures/usdt/orders"
            payload = {
                'contract': symbol,
                'size': size,
                'price': str(price) if price > 0 else '0',
                'tif': tif,
                'close': close
            }
            
            headers = self._sign('POST', url, '', json.dumps(payload))
            headers['Content-Type'] = 'application/json'
            
            response = requests.post(
                f"{self.base_url}{url}",
                headers=headers,
                json=payload
            )
            
            result = response.json()
            logging.info(f"下單成功: {result}")
            return result
        except Exception as e:
            logging.error(f"下單錯誤: {e}")
            return None
    
    def initialize_position(self, symbol, config):
        """初始化單個交易對的倉位"""
        current_price = self.get_current_price(symbol)
        if not current_price:
            return False
        
        logging.info(f"初始化 {symbol} 倉位 - 價格: {current_price}")
        
        # 計算合約數量 (簡化計算)
        long_size = int(config['long_amount'] / current_price)
        short_size = -int(config['short_amount'] / current_price)
        
        # 建立多倉
        if long_size > 0:
            self.place_futures_order(symbol, long_size)
        
        # 建立空倉
        if short_size < 0:
            self.place_futures_order(symbol, short_size)
        
        # 記錄倉位
        self.positions[symbol] = {
            'long': {
                'active': long_size > 0,
                'entry_price': current_price,
                'size': long_size,
                'add_count': 0,
                'last_add_price': current_price
            },
            'short': {
                'active': short_size < 0,
                'entry_price': current_price,
                'size': abs(short_size),
                'add_count': 0,
                'last_add_price': current_price
            },
            'config': config
        }
        
        # 記錄到資料庫
        self.db.add_trade_record(symbol, 'buy', 'long', current_price, long_size, '建倉')
        self.db.add_trade_record(symbol, 'sell', 'short', current_price, abs(short_size), '建倉')
        
        # Telegram 通知
        if self.telegram:
            msg = f"📊 <b>{symbol}</b> 建倉完成\n"
            msg += f"價格: {current_price}\n"
            msg += f"多倉: {long_size} 張\n"
            msg += f"空倉: {abs(short_size)} 張"
            self.telegram.send_message(msg)
        
        return True
    
    def check_and_trade(self, symbol):
        """檢查並執行交易邏輯"""
        if symbol not in self.positions:
            return
        
        current_price = self.get_current_price(symbol)
        if not current_price:
            return
        
        pos = self.positions[symbol]
        config = pos['config']
        
        # 檢查多倉
        if pos['long']['active']:
            self._check_long_position(symbol, current_price, pos, config)
        
        # 檢查空倉
        if pos['short']['active']:
            self._check_short_position(symbol, current_price, pos, config)
    
    def _check_long_position(self, symbol, current_price, pos, config):
        """檢查多倉"""
        long_pos = pos['long']
        
        # 檢查獲利
        profit_pct = ((current_price - long_pos['entry_price']) / long_pos['entry_price']) * 100
        
        if profit_pct >= config['take_profit']:
            # 平倉
            self.place_futures_order(symbol, -long_pos['size'], close=True)
            
            msg = f"💰 {symbol} 多倉獲利平倉\n獲利: {profit_pct:.2f}%"
            logging.info(msg)
            if self.telegram:
                self.telegram.send_message(msg)
            
            self.db.add_trade_record(symbol, 'sell', 'long', current_price, long_pos['size'], '平倉', profit_pct)
            
            # 重新建倉
            time.sleep(2)
            self.initialize_position(symbol, config)
            return
        
        # 檢查加倉
        price_change = ((current_price - long_pos['last_add_price']) / long_pos['last_add_price']) * 100
        if price_change >= config['add_threshold'] and long_pos['add_count'] < 2:
            add_size = int(config['long_amount'] / current_price)
            self.place_futures_order(symbol, add_size)
            
            long_pos['add_count'] += 1
            long_pos['last_add_price'] = current_price
            long_pos['size'] += add_size
            
            msg = f"📈 {symbol} 多倉加倉\n次數: {long_pos['add_count']}"
            logging.info(msg)
            if self.telegram:
                self.telegram.send_message(msg)
            
            self.db.add_trade_record(symbol, 'buy', 'long', current_price, add_size, f'加倉{long_pos["add_count"]}')
    
    def _check_short_position(self, symbol, current_price, pos, config):
        """檢查空倉"""
        short_pos = pos['short']
        
        # 檢查獲利
        profit_pct = ((short_pos['entry_price'] - current_price) / short_pos['entry_price']) * 100
        
        if profit_pct >= config['take_profit']:
            # 平倉
            self.place_futures_order(symbol, short_pos['size'], close=True)
            
            msg = f"💰 {symbol} 空倉獲利平倉\n獲利: {profit_pct:.2f}%"
            logging.info(msg)
            if self.telegram:
                self.telegram.send_message(msg)
            
            self.db.add_trade_record(symbol, 'buy', 'short', current_price, short_pos['size'], '平倉', profit_pct)
            
            # 重新建倉
            time.sleep(2)
            self.initialize_position(symbol, config)
            return
        
        # 檢查加倉
        price_change = ((short_pos['last_add_price'] - current_price) / short_pos['last_add_price']) * 100
        if price_change >= config['add_threshold'] and short_pos['add_count'] < 2:
            add_size = int(config['short_amount'] / current_price)
            self.place_futures_order(symbol, -add_size)
            
            short_pos['add_count'] += 1
            short_pos['last_add_price'] = current_price
            short_pos['size'] += add_size
            
            msg = f"📉 {symbol} 空倉加倉\n次數: {short_pos['add_count']}"
            logging.info(msg)
            if self.telegram:
                self.telegram.send_message(msg)
            
            self.db.add_trade_record(symbol, 'sell', 'short', current_price, add_size, f'加倉{short_pos["add_count"]}')
    
    def start_trading(self, symbols, config):
        """開始交易"""
        self.running = True
        self.active_symbols = symbols
        self.current_config = config
        
        # 初始化所有交易對
        for symbol in symbols:
            self.initialize_position(symbol, config)
        
        # 主循環
        while self.running:
            try:
                for symbol in self.active_symbols:
                    self.check_and_trade(symbol)
                
                time.sleep(60)  # 每60秒檢查一次
                
            except Exception as e:
                logging.error(f"交易循環錯誤: {e}")
                time.sleep(60)
    
    def stop_trading(self):
        """停止交易"""
        self.running = False
        if self.telegram:
            self.telegram.send_message("⛔ 交易機器人已停止")

# Flask Web 應用
app = Flask(__name__)
bot = None

@app.route('/')
def index():
    """主頁面"""
    return render_template('index.html')

@app.route('/api/contracts')
def get_contracts():
    """獲取合約列表"""
    if bot:
        contracts = bot.get_futures_contracts()
        return jsonify(contracts)
    return jsonify([])

@app.route('/api/start', methods=['POST'])
def start_bot():
    """啟動機器人"""
    global bot
    data = request.json
    
    if not bot:
        bot = GateTradingBot(
            data['api_key'],
            data['api_secret'],
            testnet=data.get('testnet', True)
        )
        
        if data.get('telegram_token') and data.get('telegram_chat_id'):
            bot.set_telegram(data['telegram_token'], data['telegram_chat_id'])
    
    # 啟動交易
    symbols = data['symbols']
    config = {
        'long_amount': data['long_amount'],
        'short_amount': data['short_amount'],
        'add_threshold': data['add_threshold'],
        'take_profit': data['take_profit']
    }
    
    thread = Thread(target=bot.start_trading, args=(symbols, config))
    thread.daemon = True
    thread.start()
    
    return jsonify({'status': 'success'})

@app.route('/api/stop', methods=['POST'])
def stop_bot():
    """停止機器人"""
    if bot:
        bot.stop_trading()
    return jsonify({'status': 'success'})

@app.route('/api/positions')
def get_positions():
    """獲取當前倉位"""
    if bot:
        positions_data = []
        for symbol, pos in bot.positions.items():
            current_price = bot.get_current_price(symbol)
            
            if pos['long']['active']:
                long_profit = ((current_price - pos['long']['entry_price']) / pos['long']['entry_price']) * 100
                positions_data.append({
                    'symbol': symbol,
                    'type': '多倉',
                    'entry_price': pos['long']['entry_price'],
                    'current_price': current_price,
                    'size': pos['long']['size'],
                    'profit': long_profit,
                    'add_count': pos['long']['add_count']
                })
            
            if pos['short']['active']:
                short_profit = ((pos['short']['entry_price'] - current_price) / pos['short']['entry_price']) * 100
                positions_data.append({
                    'symbol': symbol,
                    'type': '空倉',
                    'entry_price': pos['short']['entry_price'],
                    'current_price': current_price,
                    'size': pos['short']['size'],
                    'profit': short_profit,
                    'add_count': pos['short']['add_count']
                })
        
        return jsonify(positions_data)
    return jsonify([])

@app.route('/api/records')
def get_records():
    """獲取交易記錄"""
    if bot:
        records = bot.db.get_trade_records(100)
        return jsonify([{
            'id': r[0],
            'timestamp': r[1],
            'symbol': r[2],
            'side': r[3],
            'position_type': r[4],
            'price': r[5],
            'amount': r[6],
            'action': r[7],
            'profit': r[8],
            'notes': r[9]
        } for r in records])
    return jsonify([])

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=False)