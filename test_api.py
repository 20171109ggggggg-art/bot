# test_api.py
import gate_api
from gate_api.exceptions import ApiException, GateApiException

api_key = "你的API_KEY"
api_secret = "你的API_SECRET"

configuration = gate_api.Configuration(
    host="https://fx-api-testnet.gateio.ws/api/v4",
    key=api_key,
    secret=api_secret
)

api_client = gate_api.ApiClient(configuration)
futures_api = gate_api.FuturesApi(api_client)

try:
    # 測試獲取合約
    contracts = futures_api.list_futures_contracts('usdt')
    print(f"成功！獲取到 {len(contracts)} 個合約")
    
    # 測試獲取帳戶信息
    accounts = futures_api.list_futures_accounts('usdt')
    print(f"帳戶餘額: {accounts.total} USDT")
    
except GateApiException as e:
    print(f"API 錯誤: {e.label} - {e.message}")
except Exception as e:
    print(f"錯誤: {e}")