-- 用户表（与 `v1/Models/user.py` 中 `User` 模型一致）
-- 应用启动时若已配置 MYSQL_DB，会由 SQLAlchemy `create_all` 自动建表；也可在库中手动执行本脚本。

CREATE TABLE IF NOT EXISTS `users` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `username` VARCHAR(128) NOT NULL,
  `salt` VARCHAR(128) NOT NULL,
  `password_hash` VARCHAR(256) NOT NULL,
  `created_at` TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `ix_users_username` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
