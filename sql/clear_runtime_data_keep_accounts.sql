-- ---------------------------------------------------------------------------
-- 仅清空「运行时 / 历史业务数据」，保留帐户与配置：
--   保留：users、okx_api_accounts、follow_accounts（含 link、下注参数、OKX 绑定、启用真实交易等）
--   清空：follow_sim_records、follow_position_events、follow_position_snapshots
--
-- 执行前请备份数据库。
-- 按实际库名修改 USE（默认与 init.sql 一致；须与后端 .env 里 MYSQL_DB 一致）。
--
-- 【重要】清库后「模拟跟单资金」若很快又出现记录：
--   后端 position_monitor 会轮询对方持仓；若跟单帐户仍为「启用」且对方仍有仓位，
--   会立刻重新 INSERT follow_sim_records（不是 SQL 没生效，而是被任务写回来了）。
--   处理：执行本脚本前先 ① 停止后端进程，或 ② 临时关闭跟单（见下方可选 UPDATE），
--   清完再按需启动；或接受对方有仓时本来就会持续生成模拟行。
--
-- 若怀疑连错库，清库后在本库执行：SELECT COUNT(*) FROM follow_sim_records; 应为 0。
-- ---------------------------------------------------------------------------

SET NAMES utf8mb4;

USE okx;

-- 可选：清库前暂停全部跟单，避免监控任务在清库瞬间又把模拟行写满（不改 API 密钥等配置）
-- UPDATE `follow_accounts` SET `enabled` = 0;

-- 子表先删：模拟记录引用 events（open_event_id / close_event_id）
DELETE FROM `follow_sim_records`;

DELETE FROM `follow_position_events`;

DELETE FROM `follow_position_snapshots`;

-- 可选：让新写入的 id 从 1 起（不需要可整段注释掉）
-- ALTER TABLE `follow_sim_records` AUTO_INCREMENT = 1;
-- ALTER TABLE `follow_position_events` AUTO_INCREMENT = 1;
