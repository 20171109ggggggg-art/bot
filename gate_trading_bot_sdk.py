# gate_trading_bot_sdk.py v4.2 - 倉位塔片控制 + 持久化 + 倉位同步
import time
import requests
from datetime import datetime, timedelta
import json
import logging
from flask import Flask, render_template, request, jsonify
from threading import Thread
import sqlite3
import os

# Gate.io 官方 SDK
import gate_api
from gate_api.exceptions import ApiException, GateApiException

# 設定日誌
logging.basicConfig(
	level=logging.DEBUG,
	format='%(asctime)s - %(levelname)s - %(message)s',
	handlers=[
		logging.FileHandler('trading_bot.log', encoding='utf-8'),
		logging.StreamHandler()
	]
)

def format_price(price):
	"""智能格式化價格"""
	try:
		price = float(price)
		if price >= 1000:
			return f"{price:.2f}"
		elif price >= 1:
			return f"{price:.5f}"
		elif price >= 0.01:
			return f"{price:.6f}"
		else:
			return f"{price:.8f}"
	except (ValueError, TypeError):
		return str(price)

def calculate_next_add_price(last_add_price, is_long, threshold_pct):
	"""計算下一倉加倉價格(基於上一次加倉價格)"""
	try:
		threshold = float(threshold_pct) / 100.0
		if is_long:
			next_price = last_add_price * (1 + threshold)
		else:
			next_price = last_add_price * (1 - threshold)
		return next_price
	except:
		return None

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
			response = requests.post(url, data=data, timeout=5)
			return response.status_code == 200
		except Exception as e:
			logging.error(f"Telegram 發送失敗: {e}")
			return False

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
		"""取得交易記錄"""
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

		configuration = gate_api.Configuration(
			host="https://api-testnet.gateapi.io/api/v4" if testnet else "https://api.gateio.ws/api/v4",
			key=api_key,
			secret=api_secret
		)

		self.api_client = gate_api.ApiClient(configuration)
		self.futures_api = gate_api.FuturesApi(self.api_client)

		logging.info(f"{'測試網' if testnet else '正式網'}環境 - v4.2 倉位塔片控制 + 持久化 + 倉位同步")

		self.db = DatabaseManager()
		self.telegram = None
		self.running = False
		self.positions = {}
		self.active_symbols = []
		self.symbol_params = {}
		self.max_layer_gap = 2
		self.auto_restart_after_profit = {}
		self.manually_closed_symbols = set()

		self.stats = {
			'total_trades': 0,
			'winning_trades': 0,
			'total_profit': 0,
			'total_fees': 0,
			'last_trade_time': {},
			'daily_profit': 0,
			'daily_fees': 0,
			'daily_reset_time': datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
		}

		self.state_file = 'bot_state.json'
		self.last_sync_check = time.time()
		self.sync_check_interval = 3600

		# 啟動時自動載入狀態
		self.load_state()
		logging.info("Bot 初始化完成，已載入歷史狀態")

	def validate_params(self, params):
		"""參數驗證"""
		if not params or len(params) == 0:
			raise ValueError("參數列表不能為空")

		for i, param in enumerate(params):
			if param.get('threshold', 0) <= 0:
				raise ValueError(f"Layer {i+1} 加倉閾值必須大於0")
			if param.get('profit', 0) <= 0:
				raise ValueError(f"Layer {i+1} 獲利目標必須大於0")
			if param.get('long', 0) < 0 or param.get('short', 0) < 0:
				raise ValueError(f"Layer {i+1} 張數不能為負")

		logging.info(f"參數驗證通過: {len(params)} 層")
		return True

	def execute_with_retry(self, func, max_retries=3, *args, **kwargs):
		"""帶重試的執行函數"""
		for attempt in range(max_retries):
			try:
				return func(*args, **kwargs)
			except (GateApiException, Exception) as e:
				if attempt == max_retries - 1:
					logging.error(f"重試{max_retries}次後仍失敗: {e}")
					raise
				wait_time = 2 ** attempt
				logging.warning(f"執行失敗,{wait_time}秒後重試 (第{attempt+1}次): {e}")
				time.sleep(wait_time)

	def save_state(self):
		"""儲存狀態到檔案"""
		try:
			def convert_datetime(obj):
				"""遞歸轉換 datetime 物件為 ISO 格式字串"""
				if isinstance(obj, datetime):
					return obj.isoformat()
				elif isinstance(obj, dict):
					return {k: convert_datetime(v) for k, v in obj.items()}
				elif isinstance(obj, (list, tuple)):
					return [convert_datetime(item) for item in obj]
				elif isinstance(obj, set):
					return list(obj)
				else:
					return obj

			state = {
				'positions': convert_datetime(self.positions),
				'symbol_params': convert_datetime(self.symbol_params),
				'auto_restart_after_profit': convert_datetime(self.auto_restart_after_profit),
				'manually_closed_symbols': list(self.manually_closed_symbols),
				'stats': convert_datetime(self.stats),
				'timestamp': datetime.now().isoformat()
			}

			with open(self.state_file, 'w', encoding='utf-8') as f:
				json.dump(state, f, indent=2, ensure_ascii=False)
			logging.debug("狀態已儲存")

		except Exception as e:
			logging.error(f"儲存狀態失敗: {e}")
			import traceback
			logging.error(traceback.format_exc())

	def load_state(self):
		"""從檔案載入狀態"""
		try:
			if os.path.exists(self.state_file):
				with open(self.state_file, 'r', encoding='utf-8') as f:
					state = json.load(f)

				self.positions = state.get('positions', {})
				self.symbol_params = state.get('symbol_params', {})
				self.auto_restart_after_profit = state.get('auto_restart_after_profit', {})
				self.manually_closed_symbols = set(state.get('manually_closed_symbols', []))

				loaded_stats = state.get('stats', {})
				self.stats.update(loaded_stats)

				if 'total_fees' not in self.stats:
					self.stats['total_fees'] = 0
				if 'daily_fees' not in self.stats:
					self.stats['daily_fees'] = 0

				if 'daily_reset_time' in self.stats:
					if isinstance(self.stats['daily_reset_time'], str):
						self.stats['daily_reset_time'] = datetime.fromisoformat(self.stats['daily_reset_time'])
					elif not isinstance(self.stats['daily_reset_time'], datetime):
						self.stats['daily_reset_time'] = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

				if 'last_trade_time' not in self.stats:
					self.stats['last_trade_time'] = {}

				logging.info(f"已載入狀態: {len(self.positions)} 個倉位, {len(self.manually_closed_symbols)} 個已停止")
				logging.info(f"累計交易: {self.stats['total_trades']}, 淨盈虧: {self.stats['total_profit']:.2f} USDT, 總手續費: {self.stats.get('total_fees', 0):.2f} USDT")
				return True

		except Exception as e:
			logging.error(f"載入狀態失敗: {e}")
			import traceback
			logging.error(traceback.format_exc())

		return False

	def sync_positions_from_exchange(self):
		"""從交易所同步倉位到本地追蹤（雙向同步 - 添加和移除）"""
		try:
			synced_symbols = []
			removed_symbols = []
			
			# 直接獲取交易所所有倉位
			logging.info("正在從交易所獲取所有倉位...")
			all_positions = self.futures_api.list_positions('usdt')
			
			# 建立交易所倉位集合（只包含有倉位的交易對）
			exchange_symbols = set()
			for pos in all_positions:
				symbol = pos.contract
				size = int(pos.size) if pos.size else 0
				if size != 0:
					exchange_symbols.add(symbol)
			
			logging.info(f"交易所活躍倉位: {exchange_symbols if exchange_symbols else '無'}")
			logging.info(f"本地追蹤倉位: {set(self.positions.keys()) if self.positions else '無'}")
			
			# 1. 添加交易所有但本地沒有的倉位
			for pos in all_positions:
				symbol = pos.contract
				size = int(pos.size) if pos.size else 0
				
				# 跳過已關閉的倉位
				if size == 0:
					continue
				
				# 如果本地沒有追蹤這個倉位
				if symbol not in self.positions:
					logging.info(f"發現未追蹤的倉位: {symbol}, 大小: {size}")
					
					# 如果沒有參數配置，創建預設配置
					if symbol not in self.symbol_params:
						logging.warning(f"{symbol} 無參數配置，創建預設參數")
						default_size = abs(size)
						self.symbol_params[symbol] = [
							{'layer': 1, 'long': default_size, 'short': default_size, 
							 'threshold': 0.5, 'profit': 0.5}
						]
					
					# 獲取完整倉位信息
					exchange_pos = self.get_exchange_positions(symbol)
					
					# 重建本地倉位
					self.positions[symbol] = {
						'long': {
							'active': bool(exchange_pos['long']),
							'avg_price': exchange_pos['long']['entry_price'] if exchange_pos['long'] else 0,
							'size': exchange_pos['long']['size'] if exchange_pos['long'] else 0,
							'current_layer': 1,
							'add_count': 0,
							'last_add_price': exchange_pos['long']['entry_price'] if exchange_pos['long'] else 0,
							'total_fees': 0
						},
						'short': {
							'active': bool(exchange_pos['short']),
							'avg_price': exchange_pos['short']['entry_price'] if exchange_pos['short'] else 0,
							'size': exchange_pos['short']['size'] if exchange_pos['short'] else 0,
							'current_layer': 1,
							'add_count': 0,
							'last_add_price': exchange_pos['short']['entry_price'] if exchange_pos['short'] else 0,
							'total_fees': 0
						},
						'all_params': self.symbol_params[symbol]
					}
					
					synced_symbols.append(symbol)
					self.manually_closed_symbols.discard(symbol)
					logging.info(f"✅ {symbol} 已添加到本地追蹤")
			
			# 2. 移除本地有但交易所沒有的倉位
			local_symbols = list(self.positions.keys())
			for symbol in local_symbols:
				if symbol not in exchange_symbols:
					logging.info(f"🔍 檢測到 {symbol} 在交易所已平倉，從本地移除")
					del self.positions[symbol]
					removed_symbols.append(symbol)
			
			# 保存狀態
			if synced_symbols or removed_symbols:
				self.save_state()
				
				summary = []
				if synced_symbols:
					summary.append(f"新增追蹤: {', '.join(synced_symbols)}")
				if removed_symbols:
					summary.append(f"移除已平倉: {', '.join(removed_symbols)}")
				
				logging.info("同步完成 - " + ' | '.join(summary))
				
				if self.telegram:
					msg = "📊 倉位同步完成\n"
					if synced_symbols:
						msg += f"✅ 新增追蹤: {len(synced_symbols)} 個\n"
						for sym in synced_symbols:
							msg += f"  • {sym}\n"
					if removed_symbols:
						msg += f"❌ 移除已平倉: {len(removed_symbols)} 個\n"
						for sym in removed_symbols:
							msg += f"  • {sym}\n"
					self.telegram.send_message(msg)
				
				return {
					'success': True,
					'synced_symbols': synced_symbols,
					'removed_symbols': removed_symbols,
					'count': len(synced_symbols)
				}
			else:
				logging.info("本地倉位與交易所一致，無需同步")
				return {
					'success': True,
					'synced_symbols': [],
					'removed_symbols': [],
					'count': 0,
					'message': '本地倉位與交易所一致，無需同步'
				}
				
		except Exception as e:
			logging.error(f"同步倉位錯誤: {e}")
			import traceback
			logging.error(traceback.format_exc())
			return {
				'success': False,
				'error': str(e)
			}

	def check_daily_reset(self):
		"""檢查並執行每日統計重置"""
		now = datetime.now()
		if now.date() > self.stats['daily_reset_time'].date():
			old_profit = self.stats['daily_profit']
			old_fees = self.stats.get('daily_fees', 0)

			logging.info(f"每日統計重置 - 昨日淨盈虧: {old_profit:.2f} USDT, 手續費: {old_fees:.2f} USDT")

			self.stats['daily_profit'] = 0
			self.stats['daily_fees'] = 0
			self.stats['daily_reset_time'] = now.replace(hour=0, minute=0, second=0, microsecond=0)
			self.save_state()

			if self.telegram:
				self.telegram.send_message(
					f"每日統計重置\n"
					f"昨日淨盈虧: {old_profit:+.2f} USDT\n"
					f"昨日手續費: {old_fees:.2f} USDT\n"
					f"今日統計已重置為 0"
				)

	def set_telegram(self, bot_token, chat_id):
		"""設定 Telegram 通知"""
		self.telegram = TelegramNotifier(bot_token, chat_id)
		if self.telegram.enabled:
			self.telegram.send_message(
				"交易機器人已啟動 v4.2\n"
				"━━━━━━━━━━━━━━━\n"
				"關鍵更新:\n"
				"✅ 倉位塔片直接控制達標後行為\n"
				"✅ 設置自動持久化保存\n"
				"✅ 完整手續費追蹤\n"
				"✅ 倉位同步功能\n\n"
				"輸入 /幫助 查看可用指令"
			)

# ========== Telegram 指令功能 ==========

	def setup_telegram_commands(self):
		"""設置 Telegram 指令監聽"""
		if not self.telegram or not self.telegram.enabled:
			logging.warning("Telegram 未啟用,無法使用指令功能")
			return

		thread = Thread(target=self._telegram_command_listener)
		thread.daemon = True
		thread.start()
		logging.info("✅ Telegram 指令監聽已啟動")

	def _telegram_command_listener(self):
		"""監聽 Telegram 指令"""
		last_update_id = 0

		while self.running:
			try:
				url = f"https://api.telegram.org/bot{self.telegram.bot_token}/getUpdates"
				params = {'offset': last_update_id + 1, 'timeout': 30}
				response = requests.get(url, params=params, timeout=35)

				if response.status_code == 200:
					data = response.json()
					if data['ok'] and data['result']:
						for update in data['result']:
							last_update_id = update['update_id']

							if 'message' in update and 'text' in update['message']:
								chat_id = str(update['message']['chat']['id'])

								if chat_id != self.telegram.chat_id:
									continue

								text = update['message']['text'].strip()
								self._handle_telegram_command(text)

				time.sleep(1)

			except Exception as e:
				logging.error(f"Telegram 監聽錯誤: {e}")
				time.sleep(5)

	def _handle_telegram_command(self, command):
		"""處理 Telegram 指令"""
		logging.info(f"收到 Telegram 指令: {command}")

		if command in ['/狀態', '/status']:
			self._send_status_report()
		elif command in ['/倉位', '/positions']:
			self._send_positions_report()
		elif command in ['/盈虧', '/pnl']:
			self._send_pnl_report()
		elif command in ['/已停止', '/stopped']:
			self._send_stopped_symbols()
		elif command in ['/幫助', '/help', '/start']:
			self._send_help_message()
		else:
			self.telegram.send_message(f"未知指令: {command}\n\n輸入 /幫助 查看可用指令")

	def _send_status_report(self):
		"""發送狀態報告"""
		msg = "機器人狀態\n"
		msg += f"━━━━━━━━━━━━━━━\n"
		msg += f"運行狀態: {'🟢 運行中' if self.running else '🔴 已停止'}\n"
		msg += f"活躍交易對: {len(self.positions)} 個\n"
		msg += f"已停止交易對: {len(self.manually_closed_symbols)} 個\n"
		msg += f"總交易次數: {self.stats['total_trades']}\n"

		win_rate = (self.stats['winning_trades'] / self.stats['total_trades'] * 100) if self.stats['total_trades'] > 0 else 0
		msg += f"勝率: {win_rate:.1f}%\n"
		msg += f"累計淨盈虧: {self.stats['total_profit']:+.2f} USDT\n"
		msg += f"累計手續費: {self.stats.get('total_fees', 0):.2f} USDT\n"
		msg += f"今日淨盈虧: {self.stats['daily_profit']:+.2f} USDT\n"
		msg += f"今日手續費: {self.stats.get('daily_fees', 0):.2f} USDT\n"

		self.telegram.send_message(msg)

	def _send_positions_report(self):
		"""發送倉位報告"""
		if not self.positions:
			self.telegram.send_message("目前無活躍倉位")
			return

		msg = "當前倉位\n"
		msg += f"━━━━━━━━━━━━━━━\n"

		for symbol in self.positions.keys():
			exchange_pos = self.get_exchange_positions(symbol)
			local_pos = self.positions[symbol]

			msg += f"\n<b>{symbol}</b>\n"

			if exchange_pos['long']:
				ex_long = exchange_pos['long']
				pnl = ex_long['unrealised_pnl']
				pnl_pct = (pnl / (ex_long['entry_price'] * ex_long['size'])) * 100 if ex_long['size'] > 0 else 0
				pnl_emoji = "🟢" if pnl >= 0 else "🔴"

				msg += f"  {pnl_emoji} 多倉 Layer {local_pos['long']['current_layer']}\n"
				msg += f"    進場: {format_price(ex_long['entry_price'])}\n"
				msg += f"    現價: {format_price(ex_long['mark_price'])}\n"
				msg += f"    張數: {ex_long['size']} 張\n"
				msg += f"    盈虧: {pnl:+.2f} USDT ({pnl_pct:+.2f}%)\n"

			if exchange_pos['short']:
				ex_short = exchange_pos['short']
				pnl = ex_short['unrealised_pnl']
				pnl_pct = (pnl / (ex_short['entry_price'] * ex_short['size'])) * 100 if ex_short['size'] > 0 else 0
				pnl_emoji = "🟢" if pnl >= 0 else "🔴"

				msg += f"  {pnl_emoji} 空倉 Layer {local_pos['short']['current_layer']}\n"
				msg += f"    進場: {format_price(ex_short['entry_price'])}\n"
				msg += f"    現價: {format_price(ex_short['mark_price'])}\n"
				msg += f"    張數: {ex_short['size']} 張\n"
				msg += f"    盈虧: {pnl:+.2f} USDT ({pnl_pct:+.2f}%)\n"

		self.telegram.send_message(msg)

	def _send_pnl_report(self):
		"""發送盈虧統計"""
		total_unrealised_pnl = 0

		for symbol in self.positions.keys():
			exchange_pos = self.get_exchange_positions(symbol)

			if exchange_pos['long']:
				total_unrealised_pnl += exchange_pos['long']['unrealised_pnl']

			if exchange_pos['short']:
				total_unrealised_pnl += exchange_pos['short']['unrealised_pnl']

		pnl_emoji = "💰" if self.stats['total_profit'] >= 0 else "📉"

		msg = f"{pnl_emoji} <b>盈虧統計</b>\n"
		msg += f"━━━━━━━━━━━━━━━\n"
		msg += f"未實現盈虧: {total_unrealised_pnl:+.2f} USDT\n"
		msg += f"累計淨盈虧: {self.stats['total_profit']:+.2f} USDT\n"
		msg += f"累計手續費: {self.stats.get('total_fees', 0):.2f} USDT\n"
		msg += f"今日淨盈虧: {self.stats['daily_profit']:+.2f} USDT\n"
		msg += f"今日手續費: {self.stats.get('daily_fees', 0):.2f} USDT\n"
		msg += f"━━━━━━━━━━━━━━━\n"
		msg += f"總交易: {self.stats['total_trades']} 筆\n"
		msg += f"盈利: {self.stats['winning_trades']} 筆\n"

		win_rate = (self.stats['winning_trades'] / self.stats['total_trades'] * 100) if self.stats['total_trades'] > 0 else 0
		msg += f"勝率: {win_rate:.1f}%\n"

		self.telegram.send_message(msg)

	def _send_stopped_symbols(self):
		"""發送已停止交易對列表"""
		if not self.manually_closed_symbols:
			self.telegram.send_message("✅ 目前無已停止的交易對")
			return

		msg = "⸏ <b>已停止的交易對</b>\n"
		msg += f"━━━━━━━━━━━━━━━\n"

		for symbol in sorted(self.manually_closed_symbols):
			msg += f"  • {symbol}\n"

		msg += f"\n共 {len(self.manually_closed_symbols)} 個交易對已停止"
		msg += f"\n💡 需要在網頁手動重啟才能繼續交易"

		self.telegram.send_message(msg)

	def _send_help_message(self):
		"""發送幫助訊息"""
		msg = "🤖 <b>Telegram 指令列表</b>\n"
		msg += f"━━━━━━━━━━━━━━━\n\n"
		msg += f"📊 <b>/狀態</b> 或 <b>/status</b>\n"
		msg += f"   查看機器人運行狀態\n\n"
		msg += f"📈 <b>/倉位</b> 或 <b>/positions</b>\n"
		msg += f"   查看所有當前倉位詳情\n\n"
		msg += f"💰 <b>/盈虧</b> 或 <b>/pnl</b>\n"
		msg += f"   查看盈虧統計(含手續費)\n\n"
		msg += f"⸏ <b>/已停止</b> 或 <b>/stopped</b>\n"
		msg += f"   查看已停止的交易對\n\n"
		msg += f"❓ <b>/幫助</b> 或 <b>/help</b>\n"
		msg += f"   顯示此幫助訊息\n\n"
		msg += f"━━━━━━━━━━━━━━━\n"
		msg += f"💡 提示: 平倉等操作請使用網頁界面"

		self.telegram.send_message(msg)

	# ========== 核心交易功能 ==========

	def get_futures_contracts(self):
		"""使用官方 SDK 取得合約"""
		try:
			logging.info("使用官方 SDK 取得合約列表...")
			contracts = self.futures_api.list_futures_contracts('usdt')

			active_contracts = []
			for contract in contracts:
				if contract.name and contract.name.endswith('_USDT'):
					active_contracts.append({
						'name': contract.name,
						'underlying': getattr(contract, 'underlying', None) or contract.name.replace('_USDT', ''),
						'type': 'perpetual',
						'in_delisting': getattr(contract, 'in_delisting', False),
						'last_price': getattr(contract, 'last_price', '0') or '0'
					})

			active_contracts.sort(key=lambda x: x['name'])
			return active_contracts if active_contracts else self._get_default_contracts()

		except Exception as e:
			logging.error(f"取得合約錯誤: {e}")
			return self._get_default_contracts()

	def _get_default_contracts(self):
		"""返回預設合約列表"""
		return [
			{'name': 'BTC_USDT', 'underlying': 'BTC', 'type': 'perpetual'},
			{'name': 'ETH_USDT', 'underlying': 'ETH', 'type': 'perpetual'},
		]

	def get_current_price(self, symbol):
		"""使用官方 SDK 取得價格(帶重試)"""
		def _get_price():
			contract = self.futures_api.get_futures_contract('usdt', symbol)
			if contract and contract.last_price:
				price = float(contract.last_price)
				if price > 0:
					return price
			return None

		try:
			return self.execute_with_retry(_get_price, max_retries=3)
		except Exception as e:
			logging.error(f"取得 {symbol} 價格失敗: {e}")
			return None

	def get_exchange_positions(self, symbol):
		"""從交易所取得實際倉位資料(包含真實盈虧)"""
		try:
			positions = self.futures_api.list_positions('usdt')

			result = {'long': None, 'short': None}

			for pos in positions:
				if pos.contract != symbol:
					continue

				size = int(pos.size) if pos.size else 0

				if size == 0:
					continue

				pos_data = {
					'size': abs(size),
					'entry_price': float(pos.entry_price) if pos.entry_price else 0,
					'mark_price': float(pos.mark_price) if pos.mark_price else 0,
					'unrealised_pnl': float(pos.unrealised_pnl) if pos.unrealised_pnl else 0,
					'realised_pnl': float(pos.realised_pnl) if pos.realised_pnl else 0,
					'leverage': float(pos.leverage) if pos.leverage else 0,
				}

				if size > 0:
					result['long'] = pos_data
				else:
					result['short'] = pos_data

			return result

		except Exception as e:
			logging.error(f"取得 {symbol} 交易所倉位失敗: {e}")
			return {'long': None, 'short': None}


	def place_futures_order(self, symbol, size, price=0, tif='ioc', close=False):
		"""使用官方 SDK 下單(返回手續費)"""
		try:
			if close:
				logging.info(f"平倉訂單 - {symbol}: size={size}")
				order = gate_api.FuturesOrder(
					contract=symbol,
					size=size,
					price='0',
					tif='ioc',
					reduce_only=True
				)
			else:
				order = gate_api.FuturesOrder(
					contract=symbol,
					size=size,
					price='0' if price == 0 else str(price),
					tif=tif
				)

			result = self.futures_api.create_futures_order('usdt', order)

			if result and result.id:
				logging.info(f"下單成功 - ID: {result.id}, Status: {result.status}")

				# 獲取手續費(如果失敗則設為0)
				fee = 0
				try:
					time.sleep(0.5)
					order_detail = self.futures_api.get_futures_order('usdt', str(result.id))

					# 嘗試多種可能的屬性名稱
					if hasattr(order_detail, 'fee') and order_detail.fee:
						fee = abs(float(order_detail.fee))
					elif hasattr(order_detail, 'fill_fee') and order_detail.fill_fee:
						fee = abs(float(order_detail.fill_fee))
					elif hasattr(order_detail, 'taker_fee') and order_detail.taker_fee:
						fee = abs(float(order_detail.taker_fee))
					else:
						# 如果都沒有,估算手續費(以 0.075% 為例)
						if hasattr(order_detail, 'fill_price') and hasattr(order_detail, 'size'):
							price = float(order_detail.fill_price) if order_detail.fill_price else 0
							size_val = abs(int(order_detail.size)) if order_detail.size else 0
							fee = price * size_val * 0.00075

					logging.info(f"訂單手續費: {fee:.6f} USDT")

				except Exception as e:
					logging.warning(f"無法獲取手續費,設為 0: {e}")
					fee = 0

				return {
					'id': str(result.id),
					'status': result.status,
					'fee': fee
				}
			return None

		except Exception as e:
			logging.error(f"下單失敗: {e}")
			import traceback
			logging.error(traceback.format_exc())
			return None

	def verify_order_filled(self, order_id, symbol, max_attempts=5):
			"""驗證訂單是否成交"""
			for attempt in range(max_attempts):
				try:
					order = self.futures_api.get_futures_order('usdt', str(order_id))
					if order.status == 'finished':
						return True
					elif order.status == 'cancelled':
						return False
					time.sleep(1)
				except Exception as e:
					logging.error(f"查詢訂單錯誤: {e}")
					time.sleep(1)
			return False

	def initialize_position(self, symbol, all_params):
			"""初始化倉位(追蹤手續費)"""
			current_price = self.get_current_price(symbol)
			if not current_price:
				logging.error(f"無法取得 {symbol} 價格")
				return False

			first_valid_layer = None
			for i, param in enumerate(all_params):
				if param['long'] > 0 or param['short'] > 0:
					first_valid_layer = i
					break

			if first_valid_layer is None:
				logging.error(f"{symbol} 所有層級張數都為 0")
				return False

			first_layer = all_params[first_valid_layer]
			layer_num = first_valid_layer + 1
			long_size = first_layer['long']
			short_size = first_layer['short']

			logging.info("=" * 60)
			logging.info(f"初始化 {symbol} (Layer {layer_num})")
			logging.info(f"當前價格: {format_price(current_price)}")
			logging.info(f"多倉: {long_size} 張, 空倉: {short_size} 張")

			long_success = False
			long_fee = 0
			if long_size > 0:
				long_order = self.place_futures_order(symbol, long_size)
				if long_order and 'id' in long_order:
					if self.verify_order_filled(long_order['id'], symbol):
						long_success = True
						long_fee = long_order.get('fee', 0)

						self.stats['total_fees'] += long_fee
						self.stats['daily_fees'] += long_fee

						next_layer_idx = self._find_next_valid_layer(all_params, first_valid_layer, 'long')
						next_add_price_msg = ""
						if next_layer_idx is not None:
							threshold = all_params[first_valid_layer]['threshold']
							next_add_price = calculate_next_add_price(current_price, True, threshold)
							if next_add_price:
								next_add_price_msg = f"\n下一倉加倉價格: {format_price(next_add_price)} (Layer {next_layer_idx + 1})"

						if self.telegram:
							msg = f"<b>{symbol} 多倉建立 (Layer {layer_num})</b>\n"
							msg += f"價格: {format_price(current_price)}\n"
							msg += f"張數: {long_size}\n"
							msg += f"手續費: {long_fee:.6f} USDT"
							msg += next_add_price_msg
							self.telegram.send_message(msg)

			short_success = False
			short_fee = 0
			if short_size > 0:
				short_order = self.place_futures_order(symbol, -short_size)
				if short_order and 'id' in short_order:
					if self.verify_order_filled(short_order['id'], symbol):
						short_success = True
						short_fee = short_order.get('fee', 0)

						self.stats['total_fees'] += short_fee
						self.stats['daily_fees'] += short_fee

						next_layer_idx = self._find_next_valid_layer(all_params, first_valid_layer, 'short')
						next_add_price_msg = ""
						if next_layer_idx is not None:
							threshold = all_params[first_valid_layer]['threshold']
							next_add_price = calculate_next_add_price(current_price, False, threshold)
							if next_add_price:
								next_add_price_msg = f"\n下一倉加倉價格: {format_price(next_add_price)} (Layer {next_layer_idx + 1})"

						if self.telegram:
							msg = f"<b>{symbol} 空倉建立 (Layer {layer_num})</b>\n"
							msg += f"價格: {format_price(current_price)}\n"
							msg += f"張數: {short_size}\n"
							msg += f"手續費: {short_fee:.6f} USDT"
							msg += next_add_price_msg
							self.telegram.send_message(msg)

			if not long_success and not short_success:
				logging.error(f"{symbol} 建倉完全失敗")
				return False

			self.positions[symbol] = {
				'long': {
					'active': long_success,
					'avg_price': current_price if long_success else 0,
					'size': long_size if long_success else 0,
					'current_layer': layer_num if long_success else 0,
					'add_count': 0,
					'last_add_price': current_price if long_success else 0,
					'total_fees': long_fee
				},
				'short': {
					'active': short_success,
					'avg_price': current_price if short_success else 0,
					'size': short_size if short_success else 0,
					'current_layer': layer_num if short_success else 0,
					'add_count': 0,
					'last_add_price': current_price if short_success else 0,
					'total_fees': short_fee
				},
				'all_params': all_params
			}

			if long_success:
				self.db.add_trade_record(symbol, 'buy', 'long', current_price, long_size,
									   '建倉', layer=layer_num, notes=f'手續費:{long_fee:.6f}USDT')
			if short_success:
				self.db.add_trade_record(symbol, 'sell', 'short', current_price, short_size,
									   '建倉', layer=layer_num, notes=f'手續費:{short_fee:.6f}USDT')

			self.save_state()
			logging.info(f"建倉手續費: 多 {long_fee:.6f} / 空 {short_fee:.6f} USDT")
			logging.info("=" * 60)
			return True

	def _find_next_valid_layer(self, all_params, current_layer_idx, position_type):
			"""找到下一個有效層級索引"""
			next_idx = current_layer_idx + 1
			while next_idx < len(all_params):
				if position_type == 'long' and all_params[next_idx]['long'] > 0:
					return next_idx
				elif position_type == 'short' and all_params[next_idx]['short'] > 0:
					return next_idx
				next_idx += 1
			return None

	def check_and_trade(self, symbol):
			"""檢查並執行交易"""
			self.check_daily_reset()

			if symbol not in self.positions:
				return

			current_price = self.get_current_price(symbol)
			if not current_price:
				return

			pos = self.positions[symbol]
			all_params = pos['all_params']

			if pos['long']['active'] and pos['short']['active']:
				if self._check_hedge_profit_target(symbol, current_price, pos, all_params):
					return

			if pos['long']['active']:
				self._check_long_add_position(symbol, current_price, pos, all_params)

			if pos['short']['active']:
				self._check_short_add_position(symbol, current_price, pos, all_params)

	def _check_hedge_profit_target(self, symbol, current_price, pos, all_params):
			"""檢查對沖總體獲利目標(包含手續費計算)"""
			long_pos = pos['long']
			short_pos = pos['short']

			exchange_pos = self.get_exchange_positions(symbol)

			if not exchange_pos['long'] and not exchange_pos['short']:
				logging.warning(f"{symbol} 無法取得交易所倉位資料")
				return False

			long_pnl = exchange_pos['long']['unrealised_pnl'] if exchange_pos['long'] else 0
			short_pnl = exchange_pos['short']['unrealised_pnl'] if exchange_pos['short'] else 0
			total_pnl = long_pnl + short_pnl

			profit_pnl = max(long_pnl, short_pnl)

			if profit_pnl <= 0:
				return False

			profit_ratio = (total_pnl / profit_pnl) * 100

			current_layer_idx = max(long_pos['current_layer'], short_pos['current_layer']) - 1
			if current_layer_idx >= len(all_params):
				current_layer_idx = len(all_params) - 1

			target_profit = all_params[current_layer_idx]['profit']

			if total_pnl > 0 and profit_ratio >= target_profit:
				logging.info(f"{symbol} 達到對沖獲利目標 - 未實現盈虧: {total_pnl:.2f} USDT, 獲利率: {profit_ratio:.2f}% >= {target_profit}%")

				long_closed = False
				short_closed = False
				actual_long_size = 0
				actual_short_size = 0
				close_long_fee = 0
				close_short_fee = 0

				if exchange_pos['long']:
					actual_long_size = exchange_pos['long']['size']
					logging.info(f"準備平多倉: {actual_long_size} 張 (未實現盈虧: {long_pnl:.2f} USDT)")
					close_long = self.place_futures_order(symbol, -actual_long_size, close=True)
					if close_long and 'id' in close_long:
						long_closed = self.verify_order_filled(close_long['id'], symbol)
						if long_closed:
							close_long_fee = close_long.get('fee', 0)

				if exchange_pos['short']:
					actual_short_size = exchange_pos['short']['size']
					logging.info(f"準備平空倉: {actual_short_size} 張 (未實現盈虧: {short_pnl:.2f} USDT)")
					close_short = self.place_futures_order(symbol, actual_short_size, close=True)
					if close_short and 'id' in close_short:
						short_closed = self.verify_order_filled(close_short['id'], symbol)
						if short_closed:
							close_short_fee = close_short.get('fee', 0)

				if long_closed and short_closed:
					total_position_fees = long_pos.get('total_fees', 0) + short_pos.get('total_fees', 0)
					total_close_fees = close_long_fee + close_short_fee
					total_all_fees = total_position_fees + total_close_fees

					self.stats['total_fees'] += total_close_fees
					self.stats['daily_fees'] += total_close_fees

					net_pnl = total_pnl - total_close_fees

					auto_restart = self.auto_restart_after_profit.get(symbol, True)

					msg = f"{symbol} 對沖平倉成功\n"
					msg += f"━━━━━━━━━━━━━━━\n"
					msg += f"總獲利率: {profit_ratio:.2f}%\n"
					msg += f"未實現盈虧: {total_pnl:.2f} USDT\n"
					msg += f"平倉手續費: {total_close_fees:.6f} USDT\n"
					msg += f"建倉手續費: {total_position_fees:.6f} USDT\n"
					msg += f"總手續費: {total_all_fees:.6f} USDT\n"
					msg += f"淨盈虧: {net_pnl:.2f} USDT\n"
					msg += f"━━━━━━━━━━━━━━━\n"
					msg += f"多倉: {long_pnl:.2f} USDT ({actual_long_size}張)\n"
					msg += f"空倉: {short_pnl:.2f} USDT ({actual_short_size}張)\n"

					if auto_restart:
						msg += "\n🔄 將重新建倉繼續交易"
					else:
						msg += "\n🎯 已達標停止,不再重新建倉"

					logging.info(msg)
					if self.telegram:
						self.telegram.send_message(msg)

					self.db.add_trade_record(symbol, 'sell', 'long', current_price,
										   actual_long_size, '達標平倉', profit_ratio, long_pos['current_layer'],
										   notes=f'淨盈虧:{net_pnl:.2f}USDT,手續費:{close_long_fee:.6f}USDT')
					self.db.add_trade_record(symbol, 'buy', 'short', current_price,
										   actual_short_size, '達標平倉', profit_ratio, short_pos['current_layer'],
										   notes=f'淨盈虧:{net_pnl:.2f}USDT,手續費:{close_short_fee:.6f}USDT')

					self.stats['total_trades'] += 1
					if net_pnl > 0:
						self.stats['winning_trades'] += 1
					self.stats['total_profit'] += net_pnl
					self.stats['daily_profit'] += net_pnl
					self.stats['last_trade_time'][symbol] = datetime.now().isoformat()

					del self.positions[symbol]
					self.save_state()

					if auto_restart:
						time.sleep(2)
						self.initialize_position(symbol, all_params)
					else:
						logging.info(f"{symbol} 已達標平倉,不再建倉")
						if symbol in self.active_symbols:
							self.active_symbols.remove(symbol)

					return True
				else:
					logging.error(f"{symbol} 平倉失敗: 多倉 {'成功' if long_closed else '失敗'}, 空倉 {'成功' if short_closed else '失敗'}")

			return False

	def _check_long_add_position(self, symbol, current_price, pos, all_params):
			"""檢查多倉加倉(帶倉位層級限制 + 手續費追蹤)"""
			long_pos = pos['long']
			short_pos = pos['short']
			current_layer = long_pos['current_layer']

			if current_layer > len(all_params):
				current_layer = len(all_params)

			layer_param = all_params[current_layer - 1]

			next_layer_index = current_layer
			next_valid_layer = None

			while next_layer_index < len(all_params):
				if all_params[next_layer_index]['long'] > 0:
					next_valid_layer = next_layer_index + 1
					break
				next_layer_index += 1

			if next_valid_layer:
				if short_pos['active']:
					layer_gap = next_valid_layer - short_pos['current_layer']
					if layer_gap > self.max_layer_gap:
						logging.warning(f"{symbol} 多倉加倉受限: Layer {next_valid_layer} vs 空倉 Layer {short_pos['current_layer']}, 差距 {layer_gap} > {self.max_layer_gap}")
						if self.telegram:
							self.telegram.send_message(f"{symbol} 多倉暫停加倉\n原因: 與空倉層級差距過大({layer_gap}層)\n需等待空倉加倉")
						return

				next_param = all_params[next_valid_layer - 1]
				price_change = ((current_price - long_pos['last_add_price']) / long_pos['last_add_price']) * 100

				if price_change >= layer_param['threshold']:
					add_size = next_param['long']
					logging.info(f"{symbol} 多倉加倉 Layer {current_layer} -> {next_valid_layer}")

					add_order = self.place_futures_order(symbol, add_size)

					if add_order and 'id' in add_order:
						if self.verify_order_filled(add_order['id'], symbol):
							add_fee = add_order.get('fee', 0)

							self.stats['total_fees'] += add_fee
							self.stats['daily_fees'] += add_fee
							long_pos['total_fees'] = long_pos.get('total_fees', 0) + add_fee

							old_cost = long_pos['avg_price'] * long_pos['size']
							new_cost = current_price * add_size
							long_pos['avg_price'] = (old_cost + new_cost) / (long_pos['size'] + add_size)

							long_pos['size'] += add_size
							long_pos['current_layer'] = next_valid_layer
							long_pos['add_count'] += 1
							long_pos['last_add_price'] = current_price

							next_next_layer_idx = self._find_next_valid_layer(all_params, next_valid_layer - 1, 'long')
							next_add_price_msg = ""
							if next_next_layer_idx is not None:
								threshold = all_params[next_valid_layer - 1]['threshold']
								next_add_price = calculate_next_add_price(current_price, True, threshold)
								if next_add_price:
									next_add_price_msg = f"\n下一倉加倉價格: {format_price(next_add_price)} (Layer {next_next_layer_idx + 1})"

							if self.telegram:
								msg = f"<b>{symbol} 多倉加倉成功 (Layer {next_valid_layer})</b>\n"
								msg += f"價格: {format_price(current_price)}\n"
								msg += f"加倉張數: {add_size}\n"
								msg += f"總張數: {long_pos['size']}\n"
								msg += f"平均成本: {format_price(long_pos['avg_price'])}\n"
								msg += f"手續費: {add_fee:.6f} USDT"
								msg += next_add_price_msg
								self.telegram.send_message(msg)

							self.db.add_trade_record(symbol, 'buy', 'long', current_price,
												   add_size, f'加倉{long_pos["add_count"]}', layer=next_valid_layer,
												   notes=f'手續費:{add_fee:.6f}USDT')

							self.save_state()

	def _check_short_add_position(self, symbol, current_price, pos, all_params):
			"""檢查空倉加倉(帶倉位層級限制 + 手續費追蹤)"""
			short_pos = pos['short']
			long_pos = pos['long']
			current_layer = short_pos['current_layer']

			if current_layer > len(all_params):
				current_layer = len(all_params)

			layer_param = all_params[current_layer - 1]

			next_layer_index = current_layer
			next_valid_layer = None

			while next_layer_index < len(all_params):
				if all_params[next_layer_index]['short'] > 0:
					next_valid_layer = next_layer_index + 1
					break
				next_layer_index += 1

			if next_valid_layer:
				if long_pos['active']:
					layer_gap = next_valid_layer - long_pos['current_layer']
					if layer_gap > self.max_layer_gap:
						logging.warning(f"{symbol} 空倉加倉受限: Layer {next_valid_layer} vs 多倉 Layer {long_pos['current_layer']}, 差距 {layer_gap} > {self.max_layer_gap}")
						if self.telegram:
							self.telegram.send_message(f"{symbol} 空倉暫停加倉\n原因: 與多倉層級差距過大({layer_gap}層)\n需等待多倉加倉")
						return

				next_param = all_params[next_valid_layer - 1]
				price_change = ((short_pos['last_add_price'] - current_price) / short_pos['last_add_price']) * 100

				if price_change >= layer_param['threshold']:
					add_size = next_param['short']
					logging.info(f"{symbol} 空倉加倉 Layer {current_layer} -> {next_valid_layer}")

					add_order = self.place_futures_order(symbol, -add_size)

					if add_order and 'id' in add_order:
						if self.verify_order_filled(add_order['id'], symbol):
							add_fee = add_order.get('fee', 0)

							self.stats['total_fees'] += add_fee
							self.stats['daily_fees'] += add_fee
							short_pos['total_fees'] = short_pos.get('total_fees', 0) + add_fee

							old_cost = short_pos['avg_price'] * short_pos['size']
							new_cost = current_price * add_size
							short_pos['avg_price'] = (old_cost + new_cost) / (short_pos['size'] + add_size)

							short_pos['size'] += add_size
							short_pos['current_layer'] = next_valid_layer
							short_pos['add_count'] += 1
							short_pos['last_add_price'] = current_price

							next_next_layer_idx = self._find_next_valid_layer(all_params, next_valid_layer - 1, 'short')
							next_add_price_msg = ""
							if next_next_layer_idx is not None:
								threshold = all_params[next_valid_layer - 1]['threshold']
								next_add_price = calculate_next_add_price(current_price, False, threshold)
								if next_add_price:
									next_add_price_msg = f"\n下一倉加倉價格: {format_price(next_add_price)} (Layer {next_next_layer_idx + 1})"

							if self.telegram:
								msg = f"<b>{symbol} 空倉加倉成功 (Layer {next_valid_layer})</b>\n"
								msg += f"價格: {format_price(current_price)}\n"
								msg += f"加倉張數: {add_size}\n"
								msg += f"總張數: {short_pos['size']}\n"
								msg += f"平均成本: {format_price(short_pos['avg_price'])}\n"
								msg += f"手續費: {add_fee:.6f} USDT"
								msg += next_add_price_msg
								self.telegram.send_message(msg)

							self.db.add_trade_record(symbol, 'sell', 'short', current_price,
												   add_size, f'加倉{short_pos["add_count"]}', layer=next_valid_layer,
												   notes=f'手續費:{add_fee:.6f}USDT')

							self.save_state()

	def start_trading(self, symbols, symbol_params, auto_restart_config=None):
			"""開始交易"""
			self.running = True
			self.active_symbols = symbols
			self.symbol_params = symbol_params

			if auto_restart_config:
				self.auto_restart_after_profit = auto_restart_config
			else:
				self.auto_restart_after_profit = {symbol: True for symbol in symbols}

			logging.info(f"機器人啟動 v4.2 倉位塔片控制 + 持久化 + 倉位同步")
			logging.info(f"交易對數量: {len(symbols)}")
			logging.info(f"倉位層級限制: 最多差 {self.max_layer_gap} 層")

			for symbol in symbols:
				mode = "達標後繼續" if self.auto_restart_after_profit.get(symbol, True) else "達標後停止"
				logging.info(f"  {symbol}: {mode}")

			self.setup_telegram_commands()

			for symbol in symbols:
				params = symbol_params.get(symbol, [])
				if not params:
					logging.warning(f"{symbol} 無參數設定")
					continue

				success = self.initialize_position(symbol, params)
				if not success:
					logging.warning(f"{symbol} 初始化失敗")
				time.sleep(1)

			while self.running:
				try:
					for symbol in list(self.positions.keys()):
						self.check_and_trade(symbol)
						time.sleep(0.5)
					time.sleep(30)
				except Exception as e:
					logging.error(f"交易循環錯誤: {e}")
					import traceback
					logging.error(traceback.format_exc())
					time.sleep(60)

	def close_position(self, symbol, mode='profit'):
			"""平倉指定交易對(手動平倉後不再自動重啟 + 手續費追蹤)"""
			if symbol not in self.positions:
				return {'success': False, 'message': f'{symbol} 不存在活躍倉位'}

			pos = self.positions[symbol]
			exchange_pos = self.get_exchange_positions(symbol)

			if not exchange_pos['long'] and not exchange_pos['short']:
				return {'success': False, 'message': f'{symbol} 無實際倉位'}

			current_price = self.get_current_price(symbol)
			if not current_price:
				return {'success': False, 'message': f'{symbol} 無法取得當前價格'}

			total_pnl = 0
			long_pnl = 0
			short_pnl = 0

			if exchange_pos['long']:
				long_pnl = exchange_pos['long']['unrealised_pnl']
				total_pnl += long_pnl

			if exchange_pos['short']:
				short_pnl = exchange_pos['short']['unrealised_pnl']
				total_pnl += short_pnl

			if mode == 'profit' and total_pnl <= 0:
				return {
					'success': False,
					'message': f'{symbol} 當前虧損 {total_pnl:.2f} USDT,不符合獲利平倉條件',
					'profit': total_pnl
				}

			logging.info(f"開始平倉 {symbol} - 模式: {mode}, 未實現盈虧: {total_pnl:.2f} USDT")

			long_closed = False
			short_closed = False
			actual_long_size = 0
			actual_short_size = 0
			close_long_fee = 0
			close_short_fee = 0

			if exchange_pos['long']:
				actual_long_size = exchange_pos['long']['size']
				close_long = self.place_futures_order(symbol, -actual_long_size, close=True)
				if close_long and 'id' in close_long:
					long_closed = self.verify_order_filled(close_long['id'], symbol)
					if long_closed:
						close_long_fee = close_long.get('fee', 0)
						logging.info(f"{symbol} 多倉平倉成功: {actual_long_size} 張, 手續費: {close_long_fee:.6f} USDT")

			if exchange_pos['short']:
				actual_short_size = exchange_pos['short']['size']
				close_short = self.place_futures_order(symbol, actual_short_size, close=True)
				if close_short and 'id' in close_short:
					short_closed = self.verify_order_filled(close_short['id'], symbol)
					if short_closed:
						close_short_fee = close_short.get('fee', 0)
						logging.info(f"{symbol} 空倉平倉成功: {actual_short_size} 張, 手續費: {close_short_fee:.6f} USDT")

			if (exchange_pos['long'] and not long_closed) or (exchange_pos['short'] and not short_closed):
				return {
					'success': False,
					'message': f'{symbol} 平倉失敗: 多倉 {"成功" if long_closed else "失敗"}, 空倉 {"成功" if short_closed else "失敗"}',
					'profit': total_pnl
				}

			total_position_fees = pos['long'].get('total_fees', 0) + pos['short'].get('total_fees', 0)
			total_close_fees = close_long_fee + close_short_fee
			total_all_fees = total_position_fees + total_close_fees

			self.stats['total_fees'] += total_close_fees
			self.stats['daily_fees'] += total_close_fees

			net_pnl = total_pnl - total_close_fees

			profit_pct = (total_pnl / abs(total_pnl)) if total_pnl != 0 else 0
			action = '獲利平倉' if mode == 'profit' else '手動平倉'

			if long_closed:
				self.db.add_trade_record(symbol, 'sell', 'long', current_price,
									   actual_long_size, action, profit_pct, pos['long']['current_layer'],
									   notes=f'淨盈虧:{net_pnl:.2f}USDT,手續費:{close_long_fee:.6f}USDT')
			if short_closed:
				self.db.add_trade_record(symbol, 'buy', 'short', current_price,
									   actual_short_size, action, profit_pct, pos['short']['current_layer'],
									   notes=f'淨盈虧:{net_pnl:.2f}USDT,手續費:{close_short_fee:.6f}USDT')

			self.stats['total_trades'] += 1
			if net_pnl > 0:
				self.stats['winning_trades'] += 1
			self.stats['total_profit'] += net_pnl
			self.stats['daily_profit'] += net_pnl

			del self.positions[symbol]
			self.manually_closed_symbols.add(symbol)

			if symbol in self.active_symbols:
				self.active_symbols.remove(symbol)

			self.save_state()

			if self.telegram:
				msg = f"<b>{symbol} 平倉完成</b>\n"
				msg += f"模式: {action}\n"
				msg += f"未實現盈虧: {total_pnl:.2f} USDT\n"
				msg += f"平倉手續費: {total_close_fees:.6f} USDT\n"
				msg += f"建倉手續費: {total_position_fees:.6f} USDT\n"
				msg += f"總手續費: {total_all_fees:.6f} USDT\n"
				msg += f"淨盈虧: {net_pnl:.2f} USDT\n"
				msg += f"多倉: {actual_long_size} 張, 盈虧: {long_pnl:.2f}\n"
				msg += f"空倉: {actual_short_size} 張, 盈虧: {short_pnl:.2f}\n"
				msg += f"\n⚠️ 該交易對已停止,需手動重啟"
				self.telegram.send_message(msg)

			logging.info(f"{symbol} 手動平倉完成,已標記為停止狀態")

			return {
				'success': True,
				'message': f'{symbol} 平倉成功,淨盈虧: {net_pnl:.2f} USDT(已停止自動交易)',
				'profit': net_pnl
			}

	def close_all_positions(self, mode='profit'):
			"""平倉所有交易對(手動平倉後不再自動重啟)"""
			results = {
				'success': 0,
				'failed': 0,
				'total_profit': 0,
				'details': []
			}

			symbols_to_close = list(self.positions.keys())

			if not symbols_to_close:
				return {
					'success': 0,
					'failed': 0,
					'total_profit': 0,
					'details': [],
					'message': '無活躍倉位'
				}

			logging.info(f"開始批量平倉 - 模式: {mode}, 交易對數量: {len(symbols_to_close)}")

			for symbol in symbols_to_close:
				result = self.close_position(symbol, mode)

				if result['success']:
					results['success'] += 1
					results['total_profit'] += result.get('profit', 0)
					results['details'].append({
						'symbol': symbol,
						'status': 'success',
						'profit': result.get('profit', 0),
						'message': result['message']
					})
				else:
					results['failed'] += 1
					results['details'].append({
						'symbol': symbol,
						'status': 'failed',
						'message': result['message']
					})

				time.sleep(0.5)

			if self.telegram:
				msg = f"<b>批量平倉完成</b>\n"
				msg += f"成功: {results['success']} 個\n"
				msg += f"失敗: {results['failed']} 個\n"
				msg += f"總淨盈虧: {results['total_profit']:.2f} USDT\n"
				msg += f"\n⚠️ 所有平倉的交易對已停止,需手動重啟"
				self.telegram.send_message(msg)

			return results

	def restart_symbol(self, symbol):
			"""重新啟動已手動平倉的交易對"""
			if symbol not in self.manually_closed_symbols:
				return {
					'success': False,
					'message': f'{symbol} 不在已停止列表中'
				}

			if symbol not in self.symbol_params:
				return {
					'success': False,
					'message': f'{symbol} 缺少參數配置'
				}

			if symbol in self.positions:
				return {
					'success': False,
					'message': f'{symbol} 已有活躍倉位,無需重啟'
				}

			logging.info(f"重新啟動交易對: {symbol}")

			self.manually_closed_symbols.discard(symbol)

			if symbol not in self.active_symbols:
				self.active_symbols.append(symbol)

			params = self.symbol_params[symbol]
			success = self.initialize_position(symbol, params)

			if success:
				self.save_state()

				if self.telegram:
					self.telegram.send_message(f"✅ {symbol} 已重新啟動交易")

				return {
					'success': True,
					'message': f'{symbol} 重新啟動成功'
				}
			else:
				self.manually_closed_symbols.add(symbol)
				if symbol in self.active_symbols:
					self.active_symbols.remove(symbol)

				return {
					'success': False,
					'message': f'{symbol} 重新啟動失敗'
				}

	def stop_trading(self):
		"""停止交易"""
		self.running = False
		logging.info("機器人已停止")
		if self.telegram:
			self.telegram.send_message("🛑 交易機器人已停止")

# ========== Flask Web 應用 ==========
app = Flask(__name__)
bot = None

@app.route('/')
def index():
	return render_template('index.html')

@app.route('/api/contracts')
def get_contracts():
	try:
		testnet = request.args.get('testnet', 'true') == 'true'
		temp_config = gate_api.Configuration(
			host="https://api-testnet.gateapi.io/api/v4" if testnet else "https://api.gateio.ws/api/v4"
		)
		temp_client = gate_api.ApiClient(temp_config)
		temp_api = gate_api.FuturesApi(temp_client)
		contracts = temp_api.list_futures_contracts('usdt')

		result = []
		for c in contracts:
			if c.name and c.name.endswith('_USDT'):
				result.append({
					'name': c.name,
					'underlying': getattr(c, 'underlying', None) or c.name.replace('_USDT', ''),
					'type': 'perpetual',
					'in_delisting': getattr(c, 'in_delisting', False),
					'last_price': getattr(c, 'last_price', '0') or '0'
				})

		result.sort(key=lambda x: x['name'])
		return jsonify(result)
	except Exception as e:
		logging.error(f"取得合約錯誤: {e}")
		return jsonify([
			{'name': 'BTC_USDT', 'underlying': 'BTC', 'type': 'perpetual'},
			{'name': 'ETH_USDT', 'underlying': 'ETH', 'type': 'perpetual'},
		])

@app.route('/api/start', methods=['POST'])
def start_bot():
	global bot
	try:
		data = request.json
		if not data:
			return jsonify({'status': 'error', 'message': '未接收到資料'})

		if not bot:
			bot = GateTradingBot(
				data['api_key'],
				data['api_secret'],
				testnet=data.get('testnet', True)
			)
			if data.get('telegram_token') and data.get('telegram_chat_id'):
				bot.set_telegram(data['telegram_token'], data['telegram_chat_id'])

		symbol_params = data.get('symbol_params', {})
		symbols = data['symbols']

		for symbol in symbols:
			if symbol not in symbol_params or not symbol_params[symbol]:
				return jsonify({'status': 'error', 'message': f'{symbol} 缺少參數配置'})

		thread = Thread(target=bot.start_trading, args=(symbols, symbol_params))
		thread.daemon = True
		thread.start()

		return jsonify({
			'status': 'success',
			'message': f'已啟動 {len(symbols)} 個交易對'
		})
	except Exception as e:
		logging.error(f"啟動錯誤: {e}")
		return jsonify({'status': 'error', 'message': str(e)})

@app.route('/api/stop', methods=['POST'])
def stop_bot():
	if bot:
		bot.stop_trading()
	return jsonify({'status': 'success'})

@app.route('/api/positions')
def get_positions():
	"""取得倉位資訊(使用交易所實際資料)"""
	if bot:
		positions_data = []

		for symbol in bot.positions.keys():
			exchange_pos = bot.get_exchange_positions(symbol)
			local_pos = bot.positions[symbol]

			if exchange_pos['long']:
				ex_long = exchange_pos['long']
				profit_pct = (ex_long['unrealised_pnl'] / (ex_long['entry_price'] * ex_long['size'])) * 100 if ex_long['size'] > 0 else 0

				positions_data.append({
					'symbol': symbol,
					'type': '多倉',
					'entry_price': ex_long['entry_price'],
					'current_price': ex_long['mark_price'],
					'size': ex_long['size'],
					'profit': profit_pct,
					'unrealised_pnl': ex_long['unrealised_pnl'],
					'layer': local_pos['long']['current_layer'],
					'add_count': local_pos['long']['add_count']
				})

			if exchange_pos['short']:
				ex_short = exchange_pos['short']
				profit_pct = (ex_short['unrealised_pnl'] / (ex_short['entry_price'] * ex_short['size'])) * 100 if ex_short['size'] > 0 else 0

				positions_data.append({
					'symbol': symbol,
					'type': '空倉',
					'entry_price': ex_short['entry_price'],
					'current_price': ex_short['mark_price'],
					'size': ex_short['size'],
					'profit': profit_pct,
					'unrealised_pnl': ex_short['unrealised_pnl'],
					'layer': local_pos['short']['current_layer'],
					'add_count': local_pos['short']['add_count']
				})

		return jsonify(positions_data)
	return jsonify([])

@app.route('/api/records')
def get_records():
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
			'layer': r[9] if len(r) > 9 else 1,
			'notes': r[10] if len(r) > 10 else ''
		} for r in records])
	return jsonify([])

@app.route('/api/stats')
def get_stats():
	"""取得統計數據(包含手續費)"""
	if not bot:
		return jsonify({
			'total_trades': 0,
			'winning_trades': 0,
			'win_rate': 0,
			'total_profit': 0,
			'total_fees': 0,
			'daily_profit': 0,
			'daily_fees': 0,
			'active_positions': 0
		})

	try:
		active_count = 0
		for symbol in bot.positions.keys():
			exchange_pos = bot.get_exchange_positions(symbol)
			if exchange_pos['long']:
				active_count += 1
			if exchange_pos['short']:
				active_count += 1

		win_rate = (bot.stats['winning_trades'] / bot.stats['total_trades'] * 100) if bot.stats['total_trades'] > 0 else 0

		return jsonify({
			'total_trades': bot.stats['total_trades'],
			'winning_trades': bot.stats['winning_trades'],
			'win_rate': round(win_rate, 2),
			'total_profit': round(bot.stats['total_profit'], 2),
			'total_fees': round(bot.stats.get('total_fees', 0), 2),
			'daily_profit': round(bot.stats['daily_profit'], 2),
			'daily_fees': round(bot.stats.get('daily_fees', 0), 2),
			'active_positions': active_count
		})

	except Exception as e:
		logging.error(f"取得統計錯誤: {e}")
		return jsonify({
			'total_trades': 0,
			'winning_trades': 0,
			'win_rate': 0,
			'total_profit': 0,
			'total_fees': 0,
			'daily_profit': 0,
			'daily_fees': 0,
			'active_positions': 0
		})

@app.route('/api/close_position', methods=['POST'])
def close_position():
	"""平倉單個交易對"""
	if not bot:
		return jsonify({'success': False, 'message': '機器人未啟動'})

	try:
		data = request.json
		symbol = data.get('symbol')
		mode = data.get('mode', 'profit')

		if not symbol:
			return jsonify({'success': False, 'message': '缺少交易對參數'})

		result = bot.close_position(symbol, mode)
		return jsonify(result)

	except Exception as e:
		logging.error(f"平倉錯誤: {e}")
		return jsonify({'success': False, 'message': str(e)})

@app.route('/api/close_all_positions', methods=['POST'])
def close_all_positions():
	"""平倉所有交易對"""
	if not bot:
		return jsonify({'success': False, 'message': '機器人未啟動'})

	try:
		data = request.json
		mode = data.get('mode', 'profit')

		result = bot.close_all_positions(mode)
		return jsonify(result)

	except Exception as e:
		logging.error(f"批量平倉錯誤: {e}")
		return jsonify({'success': False, 'message': str(e)})

@app.route('/api/restart_symbol', methods=['POST'])
def restart_symbol():
	"""重新啟動已手動平倉的交易對"""
	if not bot:
		return jsonify({'success': False, 'message': '機器人未啟動'})

	try:
		data = request.json
		symbol = data.get('symbol')

		if not symbol:
			return jsonify({'success': False, 'message': '缺少交易對參數'})

		result = bot.restart_symbol(symbol)
		return jsonify(result)

	except Exception as e:
		logging.error(f"重啟交易對錯誤: {e}")
		return jsonify({'success': False, 'message': str(e)})

@app.route('/api/get_stopped_symbols')
def get_stopped_symbols():
	"""取得所有已停止的交易對"""
	if not bot:
		return jsonify([])

	try:
		stopped = list(bot.manually_closed_symbols)
		return jsonify(stopped)
	except Exception as e:
		logging.error(f"取得已停止交易對錯誤: {e}")
		return jsonify([])

@app.route('/api/update_symbol_params', methods=['POST'])
def update_symbol_params():
	"""更新指定交易對的參數(用於重啟前更新)"""
	if not bot:
		return jsonify({'success': False, 'message': '機器人未啟動'})

	try:
		data = request.json
		symbol = data.get('symbol')
		new_params = data.get('params')

		if not symbol:
			return jsonify({'success': False, 'message': '缺少交易對參數'})

		if not new_params or len(new_params) == 0:
			return jsonify({'success': False, 'message': '缺少參數配置'})

		try:
			bot.validate_params(new_params)
		except ValueError as e:
			return jsonify({'success': False, 'message': f'參數驗證失敗: {str(e)}'})

		bot.symbol_params[symbol] = new_params
		bot.save_state()

		logging.info(f"{symbol} 參數已更新: {len(new_params)} 層")

		return jsonify({
			'success': True,
			'message': f'{symbol} 參數已更新 ({len(new_params)} 層),重啟後生效'
		})

	except Exception as e:
		logging.error(f"更新參數錯誤: {e}")
		import traceback
		logging.error(traceback.format_exc())
		return jsonify({'success': False, 'message': str(e)})

@app.route('/api/get_symbol_params', methods=['GET'])
def get_symbol_params():
	"""取得指定交易對的當前參數"""
	if not bot:
		return jsonify({'success': False, 'message': '機器人未啟動'})

	try:
		symbol = request.args.get('symbol')

		if not symbol:
			return jsonify({'success': False, 'message': '缺少交易對參數'})

		if symbol not in bot.symbol_params:
			return jsonify({'success': False, 'message': f'{symbol} 無參數配置'})

		params = bot.symbol_params[symbol]

		return jsonify({
			'success': True,
			'symbol': symbol,
			'params': params
		})

	except Exception as e:
		logging.error(f"取得參數錯誤: {e}")
		return jsonify({'success': False, 'message': str(e)})

@app.route('/api/update_auto_restart', methods=['POST'])
def update_auto_restart():
	"""更新單個交易對的達標後行為"""
	if not bot:
		return jsonify({'success': False, 'message': '機器人未啟動'})

	try:
		data = request.json
		symbol = data.get('symbol')
		auto_restart = data.get('auto_restart', True)

		if not symbol:
			return jsonify({'success': False, 'message': '缺少交易對參數'})

		bot.auto_restart_after_profit[symbol] = auto_restart
		bot.save_state()

		mode_text = "達標後繼續交易" if auto_restart else "達標後停止交易"
		logging.info(f"{symbol} 設定為: {mode_text}")

		if bot.telegram:
			emoji = "🔄" if auto_restart else "🎯"
			bot.telegram.send_message(
				f"{emoji} <b>{symbol} 設定更新</b>\n"
				f"達標後行為: {mode_text}"
			)

		return jsonify({
			'success': True,
			'message': f'{symbol} 已設定為 {mode_text}'
		})

	except Exception as e:
		logging.error(f"更新達標後行為錯誤: {e}")
		return jsonify({'success': False, 'message': str(e)})

@app.route('/api/get_auto_restart/<symbol>')
def get_auto_restart(symbol):
	"""取得指定交易對的達標後行為"""
	if not bot:
		return jsonify({'success': False, 'message': '機器人未啟動'})

	try:
		auto_restart = bot.auto_restart_after_profit.get(symbol, True)
		return jsonify({
			'success': True,
			'symbol': symbol,
			'auto_restart': auto_restart
		})
	except Exception as e:
		logging.error(f"取得達標後行為錯誤: {e}")
		return jsonify({'success': False, 'message': str(e)})

@app.route('/api/sync_positions', methods=['POST'])
def sync_positions_from_exchange():
	"""從交易所同步倉位（支持臨時創建 bot）"""
	global bot
	
	try:
		temp_bot = None
		use_temp = False
		
		# 如果 bot 不存在，嘗試從請求中獲取配置創建臨時實例
		if not bot:
			data = request.json or {}
			api_key = data.get('api_key')
			api_secret = data.get('api_secret')
			testnet = data.get('testnet', True)
			
			if not api_key or not api_secret:
				return jsonify({
					'success': False,
					'message': '機器人未啟動，請填寫 API 配置'
				})
			
			logging.info("創建臨時 bot 實例用於同步倉位")
			temp_bot = GateTradingBot(api_key, api_secret, testnet)
			use_temp = True
		
		# 使用現有 bot 或臨時 bot
		target_bot = temp_bot if use_temp else bot
		result = target_bot.sync_positions_from_exchange()
		
		if result['success']:
			# 如果使用臨時 bot 且有同步結果，需要將狀態保存
			if use_temp and result['count'] > 0:
				target_bot.save_state()
				logging.info(f"臨時 bot 同步了 {result['count']} 個倉位，狀態已保存")
			
			if result['count'] > 0:
				return jsonify({
					'success': True,
					'message': f'已同步 {result["count"]} 個交易對的倉位',
					'synced_symbols': result['synced_symbols']
				})
			else:
				return jsonify({
					'success': True,
					'message': '本地倉位與交易所一致,無需同步'
				})
		else:
			return jsonify({
				'success': False,
				'message': f'同步失敗: {result.get("error", "未知錯誤")}'
			})
			
	except Exception as e:
		logging.error(f"同步倉位錯誤: {e}")
		import traceback
		logging.error(traceback.format_exc())
		return jsonify({'success': False, 'message': str(e)})

if __name__ == "__main__":
	app.run(host='0.0.0.0', port=5000, debug=False)