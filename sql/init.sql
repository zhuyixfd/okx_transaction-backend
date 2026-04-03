-- 全库表结构初始化：先删表再建表（会清空数据）。
-- 使用方式：mysql -u... -p... 目标库 < sql/init.sql

USE okx;
SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

DROP TABLE IF EXISTS `follow_sim_records`;
DROP TABLE IF EXISTS `follow_position_events`;
DROP TABLE IF EXISTS `follow_position_snapshots`;
DROP TABLE IF EXISTS `follow_accounts`;
DROP TABLE IF EXISTS `users`;

SET FOREIGN_KEY_CHECKS = 1;

CREATE TABLE `users` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `username` VARCHAR(128) NOT NULL,
  `salt` VARCHAR(128) NOT NULL,
  `password_hash` VARCHAR(256) NOT NULL,
  `created_at` TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `ix_users_username` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `follow_accounts` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `link` VARCHAR(512) NOT NULL,
  `nickname` VARCHAR(256) NULL,
  `unique_name` VARCHAR(128) NULL,
  `enabled` TINYINT(1) NOT NULL DEFAULT 0,
  `last_enabled_at` TIMESTAMP(6) NULL DEFAULT NULL,
  `created_at` TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `bet_amount_per_position` DECIMAL(24,8) NULL DEFAULT NULL,
  `max_follow_positions` INT NULL DEFAULT NULL,
  `bet_mode` VARCHAR(32) NOT NULL DEFAULT 'cost',
  `margin_ratio_threshold_pct` DECIMAL(12,4) NOT NULL DEFAULT 200.0000,
  `margin_add_ratio_of_bet` DECIMAL(12,6) NOT NULL DEFAULT 0.200000,
  `margin_auto_enabled` TINYINT(1) NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `ix_follow_accounts_link` (`link`),
  KEY `ix_follow_accounts_unique_name` (`unique_name`),
  KEY `ix_follow_accounts_last_enabled_at` (`last_enabled_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `follow_position_snapshots` (
  `follow_account_id` INT NOT NULL,
  `snapshot_json` LONGTEXT NOT NULL,
  `updated_at` TIMESTAMP(6) NOT NULL,
  PRIMARY KEY (`follow_account_id`),
  CONSTRAINT `fk_snap_follow` FOREIGN KEY (`follow_account_id`) REFERENCES `follow_accounts` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `follow_position_events` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `follow_account_id` INT NOT NULL,
  `unique_name` VARCHAR(128) NOT NULL,
  `event_type` VARCHAR(16) NOT NULL,
  `pos_id` VARCHAR(64) NULL,
  `pos_ccy` VARCHAR(32) NULL,
  `pos_side` VARCHAR(16) NULL,
  `lever` VARCHAR(32) NULL,
  `avg_px` VARCHAR(64) NULL,
  `last_px` VARCHAR(64) NULL,
  `c_time` VARCHAR(32) NULL,
  `detail_json` LONGTEXT NULL,
  `created_at` TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  KEY `ix_fpe_follow_account` (`follow_account_id`),
  KEY `ix_fpe_unique_name` (`unique_name`),
  KEY `ix_fpe_event_type` (`event_type`),
  KEY `ix_fpe_pos_id` (`pos_id`),
  KEY `ix_fpe_created` (`created_at`),
  CONSTRAINT `fk_evt_follow` FOREIGN KEY (`follow_account_id`) REFERENCES `follow_accounts` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE `follow_sim_records` (
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
