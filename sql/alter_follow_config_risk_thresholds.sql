-- 已有库升级：follow_accounts 增加 4 个风控配置字段
-- 执行：mysql -u... -p... 目标库 < sql/alter_follow_config_risk_thresholds.sql

ALTER TABLE `follow_accounts`
  ADD COLUMN `maint_margin_ratio_threshold` DECIMAL(12,6) NULL DEFAULT NULL COMMENT '维持保证金率阈值（比例，2=200%）' AFTER `margin_add_max_times`,
  ADD COLUMN `close_margin_ratio_threshold` DECIMAL(12,6) NULL DEFAULT NULL COMMENT '平仓保证金率阈值（比例，2=200%）' AFTER `maint_margin_ratio_threshold`,
  ADD COLUMN `take_profit_ratio` DECIMAL(12,6) NULL DEFAULT NULL COMMENT '止盈收益率阈值（比例，0.2=20%）' AFTER `close_margin_ratio_threshold`,
  ADD COLUMN `stop_loss_ratio` DECIMAL(12,6) NULL DEFAULT NULL COMMENT '止损收益率阈值（比例，0.1=10%）' AFTER `take_profit_ratio`;
