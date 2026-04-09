ALTER TABLE `follow_accounts`
  ADD COLUMN `open_by_asset_ratio` TINYINT(1) NOT NULL DEFAULT 0
  COMMENT '1=按资产比例开仓；0=按固定下注金额'
  AFTER `live_trading_enabled`;
