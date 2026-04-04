-- 已有库增量：跟单配置字段。执行前请备份。
-- 若你曾用含 margin_ratio_threshold_pct 的旧脚本建表，请先执行 sql/alter_drop_margin_ratio_threshold_pct.sql。
-- mysql -u... -p... 你的库名 < sql/migration_follow_config.sql

ALTER TABLE `follow_accounts`
  ADD COLUMN `bet_amount_per_position` DECIMAL(24,8) NULL DEFAULT NULL AFTER `created_at`,
  ADD COLUMN `max_follow_positions` INT NULL DEFAULT NULL AFTER `bet_amount_per_position`,
  ADD COLUMN `bet_mode` VARCHAR(32) NOT NULL DEFAULT 'cost' AFTER `max_follow_positions`,
  ADD COLUMN `margin_add_ratio_of_bet` DECIMAL(12,6) NOT NULL DEFAULT 0.200000 AFTER `bet_mode`,
  ADD COLUMN `margin_auto_enabled` TINYINT(1) NOT NULL DEFAULT 0 AFTER `margin_add_ratio_of_bet`;
