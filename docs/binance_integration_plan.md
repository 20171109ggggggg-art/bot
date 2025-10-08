# Binance Integration Plan

## Goals
- Add Binance exchange support to the existing automated trading system.
- Provide both paper-trading (simulated) and live-trading modes with a unified workflow.
- Maintain configurability, observability, and risk controls consistent with current bots.

## Mode Support Summary

| 模式 | 目的 | 交易來源 | 訂單執行 | 風控/監控 | 預期用途 |
| --- | --- | --- | --- | --- | --- |
| **Simulation (離線/回測)** | 驗證策略邏輯、回測歷史報酬 | 歷史 K 線、成交明細、行情快照 | 使用既有模擬撮合器，支援滑價、延遲參數 | 與現有 Gate.io 模擬相同：倉位上限、虧損停損、日誌 | 研發迭代、CI 回歸測試 |
| **Paper Trading (Binance Spot Testnet)** | 檢驗真實撮合流程與 API 整合 | Binance Testnet 行情 + 實時 WebSocket | 對 Testnet REST 下單，記錄撮合結果與 WebSocket 回補 | 依正式版參數執行：速率限制、持倉風險、監控告警 | 預備正式上線、夜間持續驗證 |
| **Live Trading (Production)** | 於正式市場部署資金 | Binance 正式 REST/WebSocket | 透過正式 API 下單，支援市價/限價/停損單 | 完整風控：資金上限、API 權限檢查、異常停損、觀測指標 | 實際資金交易 |

> ✅ 模擬、紙上、正式三種模式皆納入，並可透過統一設定檔與 CLI 旗標切換。

## Architectural Overview
1. **Exchange Abstraction Layer**
   - Create a Binance-specific exchange client that conforms to the existing exchange interface (REST + WebSocket capabilities).
   - Support spot trading first (extendable to futures/margin later).
   - Abstract order placement, cancellation, balance retrieval, and market data subscriptions.
2. **Mode Management**
   - Introduce a trading mode controller with three states:
     - `simulation` (backtesting/offline replay)
     - `paper` (Binance Spot Testnet)
     - `live` (production account)
   - Centralize mode selection in configuration and CLI flags.
3. **Configuration**
   - Extend config files to include Binance API keys, passphrases, base URL overrides, and trading parameters (symbol list, fee rates, min qty, etc.).
   - Provide environment-variable fallbacks to avoid secrets in source control.
4. **Data Sources**
   - Market data via Binance WebSocket streams (for ticks, order books, trades) with REST fallbacks.
   - Historical data retrieval endpoints for warm-up and simulation.

## Configuration Blueprint
1. **設定檔結構**
   ```yaml
   exchanges:
     binance:
       mode: simulation | paper | live
       rest_base_url: https://api.binance.com
       websocket_base_url: wss://stream.binance.com:9443
       api_key: ${BINANCE_API_KEY}
       api_secret: ${BINANCE_API_SECRET}
       symbols:
         - BTCUSDT
         - ETHUSDT
       order:
         max_notional: 5000
         max_position: 1.5
         time_in_force: GTC
       risk:
         max_daily_loss_pct: 4
         kill_switch_enabled: true
       testnet_overrides:
         rest_base_url: https://testnet.binance.vision
         websocket_base_url: wss://testnet.binance.vision/ws
   ```
2. **密鑰管理**
   - 使用 `.env` 或秘密管理服務儲存 API Key；程式於啟動時讀取並驗證權限（僅交易、禁止提領）。
   - 針對 paper 模式提供獨立的 Testnet Key 欄位，避免誤用正式金鑰。
3. **模式切換流程**
   - CLI 旗標 `--mode` 優先於設定檔；缺省值由設定檔的 `mode` 提供。
   - 啟動時檢查：
     1. 若為 simulation，確認本地資料來源或歷史資料快取路徑。
     2. 若為 paper，套用 `testnet_overrides` 並檢查試算 API 延遲。
     3. 若為 live，強制使用正式端點並啟動額外監控與報警。

## Paper Trading (Binance Spot Testnet)
1. **Environment Setup**
   - Use Binance Testnet base URL (`https://testnet.binance.vision`).
   - Provide scripts for generating API keys and funding testnet accounts.
2. **Order Flow**
   - Place orders via testnet REST endpoints; confirm that signed requests mirror production.
   - Handle differences (limited symbols/liquidity) and adjust strategy to avoid unrealistic fills.
3. **State Management**
   - Store simulated positions, balances, and open orders locally (SQLite/JSON) mirroring production schema.
   - Record fills and latency metrics for validation.
4. **Risk/Compliance**
   - Implement configurable max position size, max notional per order, and rate-limit guards.
   - 建立「灰階上線流程」：需連續完成 N 天紙上獲利且無錯誤，才允許切換到 live 模式。

## Live Trading
1. **Credential Management**
   - Load API keys from secure storage (env vars, vault) with encryption at rest.
   - Enforce key permission checks (read-only vs trading vs withdrawal).
2. **Order Execution**
   - Support market, limit, stop-limit orders; respect Binance lot-size, price filters, and rate limits.
   - Implement automatic retry with exponential backoff for transient errors (e.g., -1003, -1007).
3. **WebSocket Handling**
   - Subscribe to user data stream (listen key) for account/order updates; auto-renew listen key every 30 minutes.
   - Implement reconnection logic with state resync to ensure no missed fills.
4. **Risk Controls**
   - Portfolio-level exposure limits and kill switch triggered by:
     - Max daily loss
     - Loss streak threshold
     - Connectivity failure beyond timeout
   - Logging + alerting (Slack/Telegram/email) for critical events.
5. **營運手冊**
   - 提供 SOP：
     1. 切換模式前的檢核清單（API 權限、餘額、設定檔差異）。
     2. 緊急停機流程與聯絡人。
     3. 每日開盤前的健康檢查腳本（行情訂閱、訂單簿同步、延遲量測）。

## Shared Components
1. **Strategy Interface**
   - Ensure strategies receive normalized market data and account state regardless of mode.
   - Provide simulation hooks (e.g., latency injection, slippage models) when in paper mode.
2. **Backtesting Compatibility**
   - Reuse data loaders and execution simulators from existing Gate.io implementation where possible.
3. **Persistence & Telemetry**
   - Unified logging format (JSON logs).
   - Metrics via Prometheus/Grafana exporters; include per-mode tags.
4. **錯誤處理統一化**
   - 定義標準錯誤碼與重試策略（例如：`ExchangeRateLimit`, `OrderRejected`, `NetworkIssue`）。
   - 模擬模式回傳模擬錯誤，確保策略能對應實際情境。

## Testing & Validation
1. **Unit Tests**
   - Mock Binance API responses for order placement, cancellation, rate-limit errors.
   - Validate symbol filter enforcement (minNotional, lotSize, priceFilter).
2. **Integration Tests**
   - Run nightly paper trading simulations with sandbox keys.
   - Validate WebSocket reconnect logic with forced disconnects.
3. **Staging Dry-Run**
   - Execute strategies on small notional live to validate risk controls before full deployment.
4. **Monitoring**
   - Add dashboards for latency, order rejection rate, PnL per mode.
5. **驗收門檻**
   - 模擬模式：CI 需涵蓋關鍵策略回測案例並產出績效摘要。
   - 紙上模式：至少連續 72 小時無異常告警，且虧損未超過預設閾值。
   - 正式模式：首日以最小倉位啟動，並由人工審核交易紀錄後再擴大部位。

## Deliverables
- Binance exchange client module with shared abstractions.
- Configuration schema updates + sample `.env` entries.
- Scripts to bootstrap testnet credentials.
- Documentation for setup, deployment, and safety procedures.
- Runbook covering mode transitions, incident response, and monitoring dashboards。

## Timeline (High-Level)
1. **Week 1-2:** Exchange client scaffolding, configuration, unit tests.
2. **Week 3:** Paper trading mode + integration tests.
3. **Week 4:** Live trading mode hardening, risk controls, documentation.
4. **Week 5:** Staged rollout, monitoring dashboards, retrospective.
5. **Week 6:** 評估績效、優化策略參數、準備擴展至期貨或其他市場。

## 風險與緩解措施
- **市況劇烈波動**：透過自動 Kill Switch 與成交量閾值即時降風險。
- **API 變更或限制**：建立版本監控，每週檢查 Binance 公告與 API 變更日誌。
- **基礎設施故障**：支援主動式健康檢查與多區部署，並在 runbook 中預先定義故障切換流程。
- **法規要求**：定期審視所在地法規與交易所政策，必要時引入 KYC/AML 模組或限制交易對。
