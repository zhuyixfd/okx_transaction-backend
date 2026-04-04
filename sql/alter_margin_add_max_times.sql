-- 已有库升级：为 follow_accounts 增加「保证金自动追加次数上限」
-- 执行：mysql -u... -p... 目标库 < sql/alter_margin_add_max_times.sql

ALTER TABLE `follow_accounts`
  ADD COLUMN `margin_add_max_times` INT NULL DEFAULT NULL
    COMMENT '保证金自动追加次数上限，NULL=不限制'
  AFTER `margin_auto_enabled`;
