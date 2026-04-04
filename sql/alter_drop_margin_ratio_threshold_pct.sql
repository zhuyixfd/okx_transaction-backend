-- 已有库升级：删除已废弃的「保证金率阈值」列（监控侧改为固定 200%）
-- 执行：mysql -u... -p... 目标库 < sql/alter_drop_margin_ratio_threshold_pct.sql

ALTER TABLE `follow_accounts` DROP COLUMN `margin_ratio_threshold_pct`;
