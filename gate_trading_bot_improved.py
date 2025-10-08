# gate_trading_bot_improved.py
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
from decimal import Decimal

# 設定日誌
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('trading_bot.log', encoding='utf-8'),
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
            requests.post(url, data=data, timeout=5)
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
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trade_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                symbol TEXT,
                side TEXT,
                position_type TEXT,
                price REAL,
                size INTEGER,
                action TEXT,
                profit REAL,
                layer INTEGER,
                notes TEXT
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def add_trade_record(self, symbol, side, position_type, price, size, action, profit=0, layer=1, notes=''):
        """新增交易記錄"""
        conn = sqlite3.connect(self.db_name)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO trade_records (timestamp, symbol, side, position_type, price, size, action, profit, layer, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (datetime.now().isoformat(), symbol, side, position_type, price, size, action, profit, layer, notes))
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
        self.positions = {}
        self.active_symbols = []
        self.all_layer_params = []  # 儲存所有層的參數
    
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
        """獲取所有永續合約（改進版 - 不過濾）"""
        try:
            url = f"{self.base_url}/futures/usdt/contracts"
            
            logging.info(f"正在從 {url} 獲取合約列表...")
            
            headers = {
                'Accept': 'application/json',
                'Content-Type': 'application/json'
            }
            
            response = requests.get(url, headers=headers, timeout=15)
            
            if response.status_code != 200:
                logging.error(f"API 返回錯誤: {response.status_code}")
                logging.error(f"回應內容: {response.text[:500]}")
                return self._get_default_contracts()
            
            # 檢查是否為 JSON
            content_type = response.headers.get('Content-Type', '')
            if 'application/json' not in content_type:
                logging.error(f"❌ API 返回非 JSON 格式: {content_type}")
                logging.error(f"回應內容預覽: {response.text[:200]}")
                return self._get_default_contracts()
            
            try:
                contracts = response.json()
            except json.JSONDecodeError as e:
                logging.error(f"❌ JSON 解析失敗: {e}")
                logging.error(f"回應內容: {response.text[:500]}")
                return self._get_default_contracts()
            
            if not isinstance(contracts, list):
                logging.error(f"API 返回格式錯誤")
                return self._get_default_contracts()
            
            logging.info(f"📊 API 返回 {len(contracts)} 個合約")
            
            # 只過濾 USDT 永續合約，不做其他限制
            active_contracts = []
            for c in contracts:
                if isinstance(c, dict):
                    contract_name = c.get('name', '')
                    # 只檢查是否為 USDT 合約
                    if '_USDT' in contract_name:
                        contract_info = {
                            'name': contract_name,
                            'underlying': c.get('underlying', ''),
                            'type': c.get('type', 'perpetual'),
                            'in_delisting': c.get('in_delisting', False),
                            'last_price': c.get('last_price', '0'),
                            'mark_price': c.get('mark_price', '0'),
                            'quanto_multiplier': c.get('quanto_multiplier', '0.0001')
                        }
                        active_contracts.append(contract_info)
            
            # 按名稱排序
            active_contracts.sort(key=lambda x: x['name'])
            
            logging.info(f"✅ 篩選後獲得 {len(active_contracts)} 個 USDT 永續合約")
            
            # 顯示前 20 個合約作為範例
            logging.info("前 20 個合約範例:")
            for contract in active_contracts[:20]:
                status = "⚠️ 即將下市" if contract.get('in_delisting') else "✅"
                logging.info(f"  {status} {contract['name']} (價格: {contract['last_price']})")
            
            if len(active_contracts) > 20:
                logging.info(f"  ... 還有 {len(active_contracts) - 20} 個合約")
            
            if len(active_contracts) == 0:
                logging.warning("⚠️ 未獲取到任何合約，使用預設列表")
                return self._get_default_contracts()
                
            return active_contracts
            
        except requests.exceptions.Timeout:
            logging.error("⌛ API 請求超時，使用預設列表")
            return self._get_default_contracts()
        except requests.exceptions.ConnectionError:
            logging.error("❌ 網路連線錯誤，使用預設列表")
            return self._get_default_contracts()
        except Exception as e:
            logging.error(f"❌ 獲取合約列表錯誤: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            return self._get_default_contracts()
    
    def _get_default_contracts(self):
        """返回擴充的預設合約列表"""
        logging.info("📋 使用擴充預設合約列表")
        
        return [
            # 主流幣種
            {'name': 'BTC_USDT', 'underlying': 'BTC', 'type': 'perpetual'},
            {'name': 'ETH_USDT', 'underlying': 'ETH', 'type': 'perpetual'},
            {'name': 'BNB_USDT', 'underlying': 'BNB', 'type': 'perpetual'},
            {'name': 'SOL_USDT', 'underlying': 'SOL', 'type': 'perpetual'},
            {'name': 'XRP_USDT', 'underlying': 'XRP', 'type': 'perpetual'},
            
            # Layer 1
            {'name': 'ADA_USDT', 'underlying': 'ADA', 'type': 'perpetual'},
            {'name': 'AVAX_USDT', 'underlying': 'AVAX', 'type': 'perpetual'},
            {'name': 'DOT_USDT', 'underlying': 'DOT', 'type': 'perpetual'},
            {'name': 'MATIC_USDT', 'underlying': 'MATIC', 'type': 'perpetual'},
            {'name': 'ATOM_USDT', 'underlying': 'ATOM', 'type': 'perpetual'},
            
            # DeFi
            {'name': 'LINK_USDT', 'underlying': 'LINK', 'type': 'perpetual'},
            {'name': 'UNI_USDT', 'underlying': 'UNI', 'type': 'perpetual'},
            {'name': 'AAVE_USDT', 'underlying': 'AAVE', 'type': 'perpetual'},
            
            # Meme & Others
            {'name': 'DOGE_USDT', 'underlying': 'DOGE', 'type': 'perpetual'},
            {'name': 'SHIB_USDT', 'underlying': 'SHIB', 'type': 'perpetual'},
            
            # 舊幣種
            {'name': 'LTC_USDT', 'underlying': 'LTC', 'type': 'perpetual'},
            {'name': 'BCH_USDT', 'underlying': 'BCH', 'type': 'perpetual'},
            {'name': 'ETC_USDT', 'underlying': 'ETC', 'type': 'perpetual'},
            {'name': 'EOS_USDT', 'underlying': 'EOS', 'type': 'perpetual'},
            {'name': 'TRX_USDT', 'underlying': 'TRX', 'type': 'perpetual'},
            
            # Layer 2
            {'name': 'ARB_USDT', 'underlying': 'ARB', 'type': 'perpetual'},
            {'name': 'OP_USDT', 'underlying': 'OP', 'type': 'perpetual'},
            
            # Gaming & Metaverse
            {'name': 'GALA_USDT', 'underlying': 'GALA', 'type': 'perpetual'},
            {'name': 'SAND_USDT', 'underlying': 'SAND', 'type': 'perpetual'},
            {'name': 'MANA_USDT', 'underlying': 'MANA', 'type': 'perpetual'},
        ]
    
    def get_current_price(self, symbol):
        """獲取當前價格"""
        try:
            url = f"{self.base_url}/futures/usdt/contracts/{symbol}"
            
            headers = {
                'Accept': 'application/json',
                'Content-Type': 'application/json'
            }
            
            response = requests.get(url, headers=headers, timeout=10)
            
            if response.status_code == 404:
                logging.error(f"❌ 合約 {symbol} 不存在！")
                return None
            
            if response.status_code != 200:
                logging.error(f"❌ 獲取價格失敗 - 狀態碼: {response.status_code}")
                return None
            
            data = response.json()
            
            if data and 'last_price' in data:
                price = float(data.get('last_price', 0))
                if price > 0:
                    return price
                else:
                    logging.error(f"❌ {symbol} 價格為 0")
                    return None
            else:
                logging.error(f"❌ 無法從回應中獲取價格")
                return None
                
        except Exception as e:
            logging.error(f"❌ 獲取價格錯誤: {str(e)}")
            return None
    
    def place_futures_order(self, symbol, size, price=0, tif='ioc', close=False):
        """
        下永續合約單（使用張數）
        
        Args:
            symbol: 合約名稱
            size: 合約張數 (正數=做多, 負數=做空)
            price: 價格 (0=市價單)
            tif: 時效性 (ioc, gtc, poc)
            close: 是否為平倉單
        
        Returns:
            訂單結果或 None
        """
        try:
            url = "/futures/usdt/orders"
            
            # 構建訂單參數
            payload = {
                'contract': symbol,
                'size': int(size),
                'tif': tif,
                'price': '0' if price == 0 else str(price)
            }
            
            # 平倉單設定
            if close:
                payload['close'] = True
                payload['reduce_only'] = True
            
            logging.info(f"準備下單 - 合約: {symbol}, 張數: {size}, 價格: {payload['price']}, 平倉: {close}")
            
            headers = self._sign('POST', url, '', json.dumps(payload))
            headers['Content-Type'] = 'application/json'
            
            full_url = f"{self.base_url}{url}"
            
            response = requests.post(
                full_url,
                headers=headers,
                json=payload,
                timeout=10
            )
            
            logging.info(f"API 回應狀態碼: {response.status_code}")
            
            if response.status_code not in [200, 201]:
                logging.error(f"❌ 下單失敗 - 狀態碼: {response.status_code}, 錯誤: {response.text}")
                return None
            
            result = response.json()
            
            # 檢查訂單狀態
            if 'id' in result:
                order_status = result.get('status', 'unknown')
                logging.info(f"✅ 下單成功 - 訂單ID: {result['id']}, 狀態: {order_status}")
                
                # 驗證訂單是否成交
                if order_status == 'finished':
                    logging.info(f"✅ 訂單已完全成交")
                elif order_status == 'open':
                    logging.warning(f"⚠️ 訂單尚未成交，狀態: open")
                
                return result
            else:
                logging.warning(f"⚠️ 下單回應異常: {result}")
                return None
            
        except Exception as e:
            logging.error(f"❌ 下單錯誤: {str(e)}")
            import traceback
            logging.error(traceback.format_exc())
            return None
    
    def verify_order_filled(self, order_id, symbol, max_attempts=5):
        """驗證訂單是否成交"""
        for attempt in range(max_attempts):
            try:
                url = f"/futures/usdt/orders/{order_id}"
                headers = self._sign('GET', url)
                
                full_url = f"{self.base_url}{url}"
                response = requests.get(full_url, headers=headers, timeout=10)
                
                if response.status_code == 200:
                    order = response.json()
                    status = order.get('status')
                    
                    if status == 'finished':
                        logging.info(f"✅ 訂單 {order_id} 已成交")
                        return True
                    elif status == 'cancelled':
                        logging.error(f"❌ 訂單 {order_id} 已取消")
                        return False
                    else:
                        logging.info(f"⏳ 訂單 {order_id} 狀態: {status}，等待成交...")
                        time.sleep(1)
                
            except Exception as e:
                logging.error(f"驗證訂單錯誤: {e}")
            
            time.sleep(1)
        
        logging.warning(f"⚠️ 訂單 {order_id} 驗證超時")
        return False
    
    def initialize_position(self, symbol, all_params):
        """初始化單個交易對的倉位（使用第一層參數，跳過張數為0的層）"""
        current_price = self.get_current_price(symbol)
        if not current_price:
            logging.error(f"無法獲取 {symbol} 價格，跳過建倉")
            return False
        
        # 找到第一個有效層（多倉或空倉張數不為0）
        first_valid_layer = None
        for i, param in enumerate(all_params):
            if param['long'] > 0 or param['short'] > 0:
                first_valid_layer = i
                break
        
        if first_valid_layer is None:
            logging.error(f"{symbol} 所有層級的張數都為 0，無法建倉")
            return False
        
        first_layer = all_params[first_valid_layer]
        layer_num = first_valid_layer + 1
        long_size = first_layer['long']
        short_size = first_layer['short']
        
        logging.info("=" * 60)
        logging.info(f"開始初始化 {symbol} 倉位 (Layer {layer_num})")
        logging.info(f"當前價格: {current_price}")
        logging.info(f"多倉張數: {long_size}, 空倉張數: {short_size}")
        
        if long_size == 0 and short_size == 0:
            logging.error(f"{symbol} Layer {layer_num} 多空倉位張數都為 0")
            return False
        
        # 建立多倉（如果張數 > 0）
        long_success = False
        long_order_id = None
        if long_size > 0:
            logging.info(f"正在建立多倉: {long_size} 張...")
            long_order = self.place_futures_order(symbol, long_size)
            
            if long_order and 'id' in long_order:
                long_order_id = long_order['id']
                if self.verify_order_filled(long_order_id, symbol):
                    long_success = True
                    logging.info(f"多倉建立成功")
                    
                    if self.telegram:
                        msg = f"📈 <b>{symbol} 多倉建立 (Layer {layer_num})</b>\n"
                        msg += f"價格: {current_price}\n"
                        msg += f"張數: {long_size}\n"
                        msg += f"訂單ID: {long_order_id}"
                        self.telegram.send_message(msg)
                else:
                    logging.error(f"多倉訂單未成交")
            else:
                logging.error(f"多倉建立失敗")
        else:
            logging.info(f"多倉張數為 0，跳過多倉建立")
        
        # 建立空倉（如果張數 > 0）
        short_success = False
        short_order_id = None
        if short_size > 0:
            logging.info(f"正在建立空倉: {short_size} 張...")
            short_order = self.place_futures_order(symbol, -short_size)
            
            if short_order and 'id' in short_order:
                short_order_id = short_order['id']
                if self.verify_order_filled(short_order_id, symbol):
                    short_success = True
                    logging.info(f"空倉建立成功")
                    
                    if self.telegram:
                        msg = f"📉 <b>{symbol} 空倉建立 (Layer {layer_num})</b>\n"
                        msg += f"價格: {current_price}\n"
                        msg += f"張數: {short_size}\n"
                        msg += f"訂單ID: {short_order_id}"
                        self.telegram.send_message(msg)
                else:
                    logging.error(f"空倉訂單未成交")
            else:
                logging.error(f"空倉建立失敗")
        else:
            logging.info(f"空倉張數為 0，跳過空倉建立")
        
        # 檢查是否至少有一邊成功
        if not long_success and not short_success:
            logging.error(f"{symbol} 建倉完全失敗")
            return False
        
        # 記錄倉位狀態
        self.positions[symbol] = {
            'long': {
                'active': long_success,
                'entry_price': current_price if long_success else 0,
                'size': long_size if long_success else 0,
                'current_layer': layer_num if long_success else 0,
                'add_count': 0,
                'last_add_price': current_price if long_success else 0
            },
            'short': {
                'active': short_success,
                'entry_price': current_price if short_success else 0,
                'size': short_size if short_success else 0,
                'current_layer': layer_num if short_success else 0,
                'add_count': 0,
                'last_add_price': current_price if short_success else 0
            },
            'all_params': all_params
        }
        
        # 記錄到資料庫
        if long_success:
            self.db.add_trade_record(symbol, 'buy', 'long', current_price, long_size, '建倉', layer=layer_num,
                                    notes=f'{long_size} 張')
        if short_success:
            self.db.add_trade_record(symbol, 'sell', 'short', current_price, short_size, '建倉', layer=layer_num,
                                    notes=f'{short_size} 張')
        
        logging.info("=" * 60)
        
        return True
    
    def check_and_trade(self, symbol):
        """檢查並執行交易邏輯（多層參數）"""
        if symbol not in self.positions:
            return
        
        current_price = self.get_current_price(symbol)
        if not current_price:
            return
        
        pos = self.positions[symbol]
        all_params = pos['all_params']
        
        # 檢查多倉
        if pos['long']['active']:
            self._check_long_position(symbol, current_price, pos, all_params)
        
        # 檢查空倉
        if pos['short']['active']:
            self._check_short_position(symbol, current_price, pos, all_params)
    
    def _check_long_position(self, symbol, current_price, pos, all_params):
        """檢查多倉（多層邏輯）"""
        long_pos = pos['long']
        current_layer = long_pos['current_layer']
        
        # 獲取當前層的參數
        if current_layer > len(all_params):
            current_layer = len(all_params)
        
        layer_param = all_params[current_layer - 1]
        
        # 檢查獲利
        profit_pct = ((current_price - long_pos['entry_price']) / long_pos['entry_price']) * 100
        
        if profit_pct >= layer_param['profit']:
            # 平倉
            logging.info(f"💰 {symbol} 多倉達到獲利目標 {profit_pct:.2f}%，準備平倉...")
            close_order = self.place_futures_order(symbol, -long_pos['size'], close=True)
            
            if close_order and 'id' in close_order:
                # 驗證平倉成功
                if self.verify_order_filled(close_order['id'], symbol):
                    msg = f"💰 {symbol} 多倉獲利平倉 (Layer {current_layer})\n獲利: {profit_pct:.2f}%"
                    logging.info(msg)
                    if self.telegram:
                        self.telegram.send_message(msg)
                    
                    self.db.add_trade_record(symbol, 'sell', 'long', current_price, long_pos['size'], 
                                           '平倉', profit_pct, current_layer)
                    
                    # 重新建倉
                    time.sleep(2)
                    self.initialize_position(symbol, all_params)
                    return
                else:
                    logging.error(f"❌ {symbol} 多倉平倉失敗")
                    return
        
        # 檢查加倉（使用下一層參數）
        if current_layer < len(all_params):
            next_layer = current_layer + 1
            next_param = all_params[next_layer - 1]
            
            price_change = ((current_price - long_pos['last_add_price']) / long_pos['last_add_price']) * 100
            
            if price_change >= layer_param['threshold']:
                add_size = next_param['long']
                
                logging.info(f"📈 {symbol} 多倉觸發加倉 (Layer {current_layer} → {next_layer}), 加倉 {add_size} 張")
                add_order = self.place_futures_order(symbol, add_size)
                
                if add_order and 'id' in add_order:
                    if self.verify_order_filled(add_order['id'], symbol):
                        long_pos['current_layer'] = next_layer
                        long_pos['add_count'] += 1
                        long_pos['last_add_price'] = current_price
                        long_pos['size'] += add_size
                        
                        msg = f"📈 {symbol} 多倉加倉成功\nLayer: {next_layer}\n張數: +{add_size}"
                        logging.info(msg)
                        if self.telegram:
                            self.telegram.send_message(msg)
                        
                        self.db.add_trade_record(symbol, 'buy', 'long', current_price, add_size, 
                                               f'加倉{long_pos["add_count"]}', layer=next_layer)
                    else:
                        logging.error(f"❌ {symbol} 多倉加倉失敗")
    
    def _check_short_position(self, symbol, current_price, pos, all_params):
        """檢查空倉（多層邏輯）"""
        short_pos = pos['short']
        current_layer = short_pos['current_layer']
        
        # 獲取當前層的參數
        if current_layer > len(all_params):
            current_layer = len(all_params)
        
        layer_param = all_params[current_layer - 1]
        
        # 檢查獲利
        profit_pct = ((short_pos['entry_price'] - current_price) / short_pos['entry_price']) * 100
        
        if profit_pct >= layer_param['profit']:
            # 平倉
            logging.info(f"💰 {symbol} 空倉達到獲利目標 {profit_pct:.2f}%，準備平倉...")
            close_order = self.place_futures_order(symbol, short_pos['size'], close=True)
            
            if close_order and 'id' in close_order:
                # 驗證平倉成功
                if self.verify_order_filled(close_order['id'], symbol):
                    msg = f"💰 {symbol} 空倉獲利平倉 (Layer {current_layer})\n獲利: {profit_pct:.2f}%"
                    logging.info(msg)
                    if self.telegram:
                        self.telegram.send_message(msg)
                    
                    self.db.add_trade_record(symbol, 'buy', 'short', current_price, short_pos['size'], 
                                           '平倉', profit_pct, current_layer)
                    
                    # 重新建倉
                    time.sleep(2)
                    self.initialize_position(symbol, all_params)
                    return
                else:
                    logging.error(f"❌ {symbol} 空倉平倉失敗")
                    return
        
        # 檢查加倉（使用下一層參數）
        if current_layer < len(all_params):
            next_layer = current_layer + 1
            next_param = all_params[next_layer - 1]
            
            price_change = ((short_pos['last_add_price'] - current_price) / short_pos['last_add_price']) * 100
            
            if price_change >= layer_param['threshold']:
                add_size = next_param['short']
                
                logging.info(f"📉 {symbol} 空倉觸發加倉 (Layer {current_layer} → {next_layer}), 加倉 {add_size} 張")
                add_order = self.place_futures_order(symbol, -add_size)
                
                if add_order and 'id' in add_order:
                    if self.verify_order_filled(add_order['id'], symbol):
                        short_pos['current_layer'] = next_layer
                        short_pos['add_count'] += 1
                        short_pos['last_add_price'] = current_price
                        short_pos['size'] += add_size
                        
                        msg = f"📉 {symbol} 空倉加倉成功\nLayer: {next_layer}\n張數: +{add_size}"
                        logging.info(msg)
                        if self.telegram:
                            self.telegram.send_message(msg)
                        
                        self.db.add_trade_record(symbol, 'sell', 'short', current_price, add_size, 
                                               f'加倉{short_pos["add_count"]}', layer=next_layer)
                    else:
                        logging.error(f"❌ {symbol} 空倉加倉失敗")
    
    def start_trading(self, symbols, all_layer_params):
        """開始交易"""
        self.running = True
        self.active_symbols = symbols
        self.all_layer_params = all_layer_params
        
        logging.info(f"🚀 機器人啟動 - 交易對: {symbols}")
        logging.info(f"📊 使用 {len(all_layer_params)} 層參數")
        
        # 初始化所有交易對
        for symbol in symbols:
            success = self.initialize_position(symbol, all_layer_params)
            if not success:
                logging.warning(f"⚠️ {symbol} 初始化失敗，將跳過此交易對")
            time.sleep(1)  # 避免 API 請求過於頻繁
        
        # 主循環
        while self.running:
            try:
                for symbol in list(self.positions.keys()):
                    self.check_and_trade(symbol)
                    time.sleep(0.5)  # 每個交易對之間間隔
                
                time.sleep(60)  # 每60秒檢查一次
                
            except Exception as e:
                logging.error(f"交易循環錯誤: {e}")
                import traceback
                logging.error(traceback.format_exc())
                time.sleep(60)
    
    def stop_trading(self):
        """停止交易"""
        self.running = False
        logging.info("⛔ 機器人已停止")
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
    """獲取合約列表（改進版 - 返回所有合約）"""
    try:
        testnet = request.args.get('testnet', 'true') == 'true'
        
        # 使用公開 API 端點
        if testnet:
            url = "https://fx-api-testnet.gateio.ws/api/v4/futures/usdt/contracts"
        else:
            url = "https://api.gateio.ws/api/v4/futures/usdt/contracts"
        
        logging.info(f"正在從公開 API 獲取合約: {url}")
        
        headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        }
        
        response = requests.get(url, headers=headers, timeout=15)
        
        if response.status_code != 200:
            logging.error(f"API 返回錯誤: {response.status_code}")
            return jsonify(get_default_contracts_list())
        
        contracts = response.json()
        
        if not isinstance(contracts, list):
            logging.error("API 返回格式錯誤")
            return jsonify(get_default_contracts_list())
        
        logging.info(f"📊 獲取到 {len(contracts)} 個原始合約")
        
        # 只篩選 USDT 永續合約，不做其他限制
        active_contracts = []
        for c in contracts:
            if isinstance(c, dict):
                contract_name = c.get('name', '')
                if '_USDT' in contract_name:
                    active_contracts.append({
                        'name': contract_name,
                        'underlying': c.get('underlying', ''),
                        'type': c.get('type', 'perpetual'),
                        'in_delisting': c.get('in_delisting', False),
                        'last_price': c.get('last_price', '0')
                    })
        
        # 按名稱排序
        active_contracts.sort(key=lambda x: x['name'])
        
        logging.info(f"✅ 篩選出 {len(active_contracts)} 個 USDT 永續合約")
        
        if len(active_contracts) == 0:
            logging.warning("未獲取到合約，返回預設列表")
            return jsonify(get_default_contracts_list())
        
        return jsonify(active_contracts)
        
    except Exception as e:
        logging.error(f"獲取合約錯誤: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return jsonify(get_default_contracts_list())

def get_default_contracts_list():
    """返回擴充預設合約列表（用於 API）"""
    return [
        {'name': 'BTC_USDT', 'underlying': 'BTC', 'type': 'perpetual'},
        {'name': 'ETH_USDT', 'underlying': 'ETH', 'type': 'perpetual'},
        {'name': 'BNB_USDT', 'underlying': 'BNB', 'type': 'perpetual'},
        {'name': 'SOL_USDT', 'underlying': 'SOL', 'type': 'perpetual'},
        {'name': 'XRP_USDT', 'underlying': 'XRP', 'type': 'perpetual'},
        {'name': 'ADA_USDT', 'underlying': 'ADA', 'type': 'perpetual'},
        {'name': 'AVAX_USDT', 'underlying': 'AVAX', 'type': 'perpetual'},
        {'name': 'DOT_USDT', 'underlying': 'DOT', 'type': 'perpetual'},
        {'name': 'MATIC_USDT', 'underlying': 'MATIC', 'type': 'perpetual'},
        {'name': 'LINK_USDT', 'underlying': 'LINK', 'type': 'perpetual'},
        {'name': 'UNI_USDT', 'underlying': 'UNI', 'type': 'perpetual'},
        {'name': 'DOGE_USDT', 'underlying': 'DOGE', 'type': 'perpetual'},
        {'name': 'LTC_USDT', 'underlying': 'LTC', 'type': 'perpetual'},
        {'name': 'ARB_USDT', 'underlying': 'ARB', 'type': 'perpetual'},
        {'name': 'OP_USDT', 'underlying': 'OP', 'type': 'perpetual'},
        {'name': 'GALA_USDT', 'underlying': 'GALA', 'type': 'perpetual'},
        {'name': 'SAND_USDT', 'underlying': 'SAND', 'type': 'perpetual'},
    ]

@app.route('/api/start', methods=['POST'])
def start_bot():
    """啟動機器人（改進版 - 使用所有層參數）"""
    global bot
    
    try:
        data = request.json
        
        if not data:
            return jsonify({'status': 'error', 'message': '未接收到數據'})
        
        logging.info(f"收到啟動請求，數據: {data.keys()}")
        
        # 檢查必要參數
        if 'api_key' not in data or 'api_secret' not in data:
            return jsonify({'status': 'error', 'message': '缺少 API 金鑰'})
        
        if 'symbols' not in data or len(data['symbols']) == 0:
            return jsonify({'status': 'error', 'message': '未選擇交易對'})
        
        if 'all_params' not in data or len(data['all_params']) == 0:
            return jsonify({'status': 'error', 'message': '未提供參數設定'})
        
        # 創建或重用 bot 實例
        if not bot:
            bot = GateTradingBot(
                data['api_key'],
                data['api_secret'],
                testnet=data.get('testnet', True)
            )
            
            if data.get('telegram_token') and data.get('telegram_chat_id'):
                bot.set_telegram(data['telegram_token'], data['telegram_chat_id'])
        
        # 獲取所有層的參數
        all_params = data['all_params']
        symbols = data['symbols']
        
        logging.info(f"啟動機器人 - 交易對: {len(symbols)} 個, 參數層數: {len(all_params)} 層")
        
        # 啟動交易
        thread = Thread(target=bot.start_trading, args=(symbols, all_params))
        thread.daemon = True
        thread.start()
        
        return jsonify({
            'status': 'success',
            'message': f'機器人已啟動，共 {len(symbols)} 個交易對，{len(all_params)} 層參數'
        })
        
    except KeyError as e:
        logging.error(f"缺少必要參數: {e}")
        return jsonify({'status': 'error', 'message': f'缺少必要參數: {str(e)}'})
    except Exception as e:
        logging.error(f"啟動機器人錯誤: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return jsonify({'status': 'error', 'message': f'啟動失敗: {str(e)}'})

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
            
            if not current_price:
                continue
            
            if pos['long']['active']:
                long_profit = ((current_price - pos['long']['entry_price']) / pos['long']['entry_price']) * 100
                positions_data.append({
                    'symbol': symbol,
                    'type': '多倉',
                    'entry_price': pos['long']['entry_price'],
                    'current_price': current_price,
                    'size': pos['long']['size'],
                    'profit': long_profit,
                    'layer': pos['long']['current_layer'],
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
                    'layer': pos['short']['current_layer'],
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
            'size': r[6],
            'action': r[7],
            'profit': r[8],
            'layer': r[9],
            'notes': r[10]
        } for r in records])
    return jsonify([])

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=False)