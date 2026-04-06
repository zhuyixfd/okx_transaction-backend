-- 已有库升级：follow_sim_records 增加累计投入字段
-- 执行：mysql -u... -p... 目标库 < sql/alter_follow_sim_record_total_invested.sql

ALTER TABLE `follow_sim_records`
  ADD COLUMN `total_invested_usdt` DECIMAL(24,8) NOT NULL DEFAULT 0.00000000 AFTER `add_margin_count`;
