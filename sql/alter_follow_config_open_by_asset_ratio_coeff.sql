ALTER TABLE `follow_accounts`
  ADD COLUMN `open_by_asset_ratio_coeff` DECIMAL(12,6) NOT NULL DEFAULT 1.000000
  COMMENT '按资产比例开仓系数（默认1）'
  AFTER `open_by_asset_ratio`;
