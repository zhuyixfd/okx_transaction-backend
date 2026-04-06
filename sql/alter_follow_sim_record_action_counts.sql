-- 已有库升级：follow_sim_records 增加持仓操作计数字段
-- 执行：mysql -u... -p... 目标库 < sql/alter_follow_sim_record_action_counts.sql

ALTER TABLE `follow_sim_records`
  ADD COLUMN `add_position_count` INT NOT NULL DEFAULT 0 AFTER `src_liq_px`,
  ADD COLUMN `reduce_position_count` INT NOT NULL DEFAULT 0 AFTER `add_position_count`,
  ADD COLUMN `add_margin_count` INT NOT NULL DEFAULT 0 AFTER `reduce_position_count`;
