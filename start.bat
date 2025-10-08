@echo off
chcp 65001 >nul
echo ========================================
echo   Gate.io 交易機器人啟動程式
echo ========================================
echo.

echo [1/3] 檢查 Python 環境...
python --version
if errorlevel 1 (
    echo ❌ 未安裝 Python，請先安裝 Python 3.7 以上版本
    pause
    exit
)
echo ✅ Python 環境正常
echo.

echo [2/3] 檢查依賴套件...
python -c "import flask" 2>nul
if errorlevel 1 (
    echo ⚠️  缺少依賴，正在安裝...
    pip install -r requirements.txt
) else (
    echo ✅ 依賴套件已安裝
)
echo.

echo [3/3] 啟動交易機器人...
echo.
echo ========================================
echo   服務已啟動！
echo   請開啟瀏覽器訪問：
echo   http://localhost:5000
echo ========================================
echo.
echo 按 Ctrl+C 可停止程式
echo.

python gate_trading_bot_sdk.py

pause