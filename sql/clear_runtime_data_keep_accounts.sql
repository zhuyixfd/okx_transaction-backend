-- ---------------------------------------------------------------------------
-- 仅清空「运行时 / 历史业务数据」，保留帐户与配置：
--   保留：users、okx_api_accounts、follow_accounts（含 link、下注参数、OKX 绑定、启用真实交易等）
--   清空：follow_sim_records、follow_position_events、follow_position_snapshots
--
-- 执行前请备份数据库。
-- 按实际库名修改 USE（默认与 init.sql 一致）。
-- ---------------------------------------------------------------------------

SET NAMES utf8mb4;

USE okx;

-- 子表先删：模拟记录引用 events（open_event_id / close_event_id）
DELETE FROM `follow_sim_records`;

DELETE FROM `follow_position_events`;

DELETE FROM `follow_position_snapshots`;

-- 可选：让新写入的 id 从 1 起（不需要可整段注释掉）
-- ALTER TABLE `follow_sim_records` AUTO_INCREMENT = 1;
-- ALTER TABLE `follow_position_events` AUTO_INCREMENT = 1;
