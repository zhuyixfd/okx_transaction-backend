-- 已有库增量：模拟跟单资金记录表。执行前请备份。
-- mysql -u... -p... 你的库名 < sql/migration_follow_sim_records.sql

CREATE TABLE IF NOT EXISTS `follow_sim_records` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `follow_account_id` INT NOT NULL,
  `pos_id` VARCHAR(64) NOT NULL,
  `pos_ccy` VARCHAR(32) NULL,
  `pos_side` VARCHAR(16) NULL,
  `entry_avg_px` VARCHAR(64) NULL,
  `stake_usdt` DECIMAL(24,8) NOT NULL DEFAULT 0.00000000,
  `status` VARCHAR(16) NOT NULL,
  `open_event_id` INT NULL,
  `close_event_id` INT NULL,
  `exit_px` VARCHAR(64) NULL,
  `realized_pnl_usdt` DECIMAL(24,8) NULL,
  `unrealized_pnl_usdt` DECIMAL(24,8) NOT NULL DEFAULT 0.00000000,
  `last_mark_px` VARCHAR(64) NULL,
  `opened_at` TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `closed_at` TIMESTAMP(6) NULL DEFAULT NULL,
  `updated_at` TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  KEY `ix_fsr_follow_account` (`follow_account_id`),
  KEY `ix_fsr_pos_id` (`pos_id`),
  KEY `ix_fsr_status` (`status`),
  CONSTRAINT `fk_fsr_follow` FOREIGN KEY (`follow_account_id`) REFERENCES `follow_accounts` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_fsr_open_evt` FOREIGN KEY (`open_event_id`) REFERENCES `follow_position_events` (`id`) ON DELETE SET NULL,
  CONSTRAINT `fk_fsr_close_evt` FOREIGN KEY (`close_event_id`) REFERENCES `follow_position_events` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
