-- 增量脚本：为 follow_sim_records 增加最近一次下单失败原因字段
-- 执行前请先选择对应数据库（USE your_db;）

ALTER TABLE `follow_sim_records`
  ADD COLUMN `live_last_error` VARCHAR(1024) NULL AFTER `live_close_ok`;

