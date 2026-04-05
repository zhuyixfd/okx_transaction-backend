-- 交易员跟单：是否启用真实交易（私有接口下单/追加）
SET NAMES utf8mb4;

ALTER TABLE `follow_accounts`
  ADD COLUMN `live_trading_enabled` TINYINT(1) NOT NULL DEFAULT 0
  AFTER `okx_api_account_id`;
