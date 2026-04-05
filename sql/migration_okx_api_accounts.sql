-- 已有库增量：OKX API 多帐户表 + 交易员跟单绑定列
-- 执行前请备份；按库名修改 USE。

SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS `okx_api_accounts` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `api_key` VARCHAR(256) NOT NULL,
  `api_secret` LONGTEXT NOT NULL,
  `api_passphrase` VARCHAR(512) NOT NULL,
  `api_label` VARCHAR(256) NULL,
  `remark` VARCHAR(512) NULL,
  `created_at` TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

ALTER TABLE `follow_accounts`
  ADD COLUMN `okx_api_account_id` INT NULL DEFAULT NULL AFTER `margin_add_max_times`;

ALTER TABLE `follow_accounts`
  ADD UNIQUE KEY `uq_follow_accounts_okx_api` (`okx_api_account_id`),
  ADD CONSTRAINT `fk_follow_okx_api` FOREIGN KEY (`okx_api_account_id`) REFERENCES `okx_api_accounts` (`id`) ON DELETE RESTRICT;
