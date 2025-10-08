import gate_api
from gate_api.exceptions import GateApiException

api_key = "9f5f199300a42daf615dc5f16b1bba64"
api_secret = "8296c44034b58c073cbd1e8fa7a0b5997339fbe34857db76e62d5e0a894b94d9"

# 使用正確的測試網域名
configuration = gate_api.Configuration(
    host="https://api-testnet.gateapi.io/api/v4",  # 修正域名
    key=api_key,
    secret=api_secret
)

api_client = gate_api.ApiClient(configuration)
futures_api = gate_api.FuturesApi(api_client)

print("測試 1: 獲取合約...")
try:
    contracts = futures_api.list_futures_contracts('usdt')
    print(f"成功！獲取到 {len(contracts)} 個合約\n")
except Exception as e:
    print(f"失敗: {e}\n")

print("測試 2: 測試下單...")
try:
    order = gate_api.FuturesOrder(
        contract='BTC_USDT',
        size=1,
        price='0',
        tif='ioc'
    )
    
    result = futures_api.create_futures_order('usdt', order)
    print(f"下單成功！訂單 ID: {result.id}")
    print(f"狀態: {result.status}\n")
    print("API 權限完全正常！\n")
    
except GateApiException as e:
    print(f"錯誤: {e.label} - {e.message}\n")