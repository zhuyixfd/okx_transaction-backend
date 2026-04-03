-- 合并「跟单仓位数量」与「只跟 n 个」为单一字段 max_follow_positions 后，删除冗余列。
-- 若库中无该列会报错，可忽略或先检查 INFORMATION_SCHEMA。

ALTER TABLE `follow_accounts` DROP COLUMN `follow_position_count`;
