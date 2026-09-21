-- Verified source definitions only; no data or credentials.
-- The rehearsal changes ONLY the final charset/collation clause.

-- sport: mlb

CREATE TABLE `analytics_dirty_venue` (
  `venue` varchar(300) NOT NULL,
  `revision` int NOT NULL,
  `dirty` tinyint(1) NOT NULL,
  `updated_at` datetime(6) NOT NULL,
  PRIMARY KEY (`venue`),
  KEY `ix_analytics_dirty_venue_dirty` (`dirty`,`updated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3;

CREATE TABLE `event` (
  `id` int NOT NULL AUTO_INCREMENT,
  `title` varchar(700) NOT NULL,
  `event_date` datetime(6) NOT NULL,
  `event_sections` json NOT NULL,
  `URL` varchar(700) DEFAULT NULL,
  `Place` varchar(300) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=313 DEFAULT CHARSET=utf8mb3;

CREATE TABLE `iterations` (
  `id` int NOT NULL AUTO_INCREMENT,
  `event_id` int NOT NULL,
  `captured_at` datetime(6) NOT NULL,
  PRIMARY KEY (`id`),
  KEY `event_id` (`event_id`),
  CONSTRAINT `iterations_ibfk_1` FOREIGN KEY (`event_id`) REFERENCES `event` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=40496 DEFAULT CHARSET=utf8mb3;

CREATE TABLE `section_bucket_summary` (
  `event_id` int NOT NULL,
  `section_key` varchar(600) NOT NULL,
  `bucket_slot` int NOT NULL,
  `section_name` varchar(300) NOT NULL,
  `median_price` float NOT NULL,
  `observation_count` int NOT NULL,
  `first_captured_at` datetime(6) NOT NULL,
  `last_captured_at` datetime(6) NOT NULL,
  `refreshed_at` datetime(6) NOT NULL,
  PRIMARY KEY (`event_id`,`section_key`,`bucket_slot`),
  KEY `ix_section_bucket_summary_event` (`event_id`),
  KEY `ix_section_bucket_summary_key_event` (`section_key`,`event_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3;

CREATE TABLE `section_summary_state` (
  `event_id` int NOT NULL AUTO_INCREMENT,
  `summary_version` int NOT NULL,
  `event_signature` varchar(64) NOT NULL,
  `source_iteration_id` int DEFAULT NULL,
  `source_iteration_count` int NOT NULL,
  `complete` tinyint(1) NOT NULL,
  `refreshed_at` datetime(6) NOT NULL,
  PRIMARY KEY (`event_id`)
) ENGINE=InnoDB AUTO_INCREMENT=313 DEFAULT CHARSET=utf8mb3;

CREATE TABLE `team_report_summary` (
  `sport` varchar(16) NOT NULL,
  `venue` varchar(191) NOT NULL,
  `season` int NOT NULL,
  `summary_version` int NOT NULL,
  `source_revision` int NOT NULL,
  `payload` json NOT NULL,
  `refreshed_at` datetime NOT NULL,
  PRIMARY KEY (`sport`,`venue`,`season`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3;

CREATE TABLE `tickets` (
  `id` int NOT NULL AUTO_INCREMENT,
  `section` varchar(300) NOT NULL,
  `price` int NOT NULL,
  `ticketsPerSection` int DEFAULT NULL,
  `iteration_id` int NOT NULL,
  PRIMARY KEY (`id`),
  KEY `iteration_id` (`iteration_id`),
  CONSTRAINT `tickets_ibfk_1` FOREIGN KEY (`iteration_id`) REFERENCES `iterations` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=5759850 DEFAULT CHARSET=utf8mb3;

-- sport: nfl

CREATE TABLE `analytics_dirty_venue` (
  `venue` varchar(300) NOT NULL,
  `revision` int NOT NULL,
  `dirty` tinyint(1) NOT NULL,
  `updated_at` datetime(6) NOT NULL,
  PRIMARY KEY (`venue`),
  KEY `ix_analytics_dirty_venue_dirty` (`dirty`,`updated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3;

CREATE TABLE `nfl_event` (
  `id` int NOT NULL AUTO_INCREMENT,
  `source_id` varchar(191) NOT NULL,
  `title` varchar(700) NOT NULL,
  `event_date` datetime(6) NOT NULL,
  `sections` json NOT NULL,
  `source_url` varchar(700) NOT NULL,
  `venue` varchar(300) NOT NULL,
  `schedule_id` varchar(191) DEFAULT NULL,
  `away_team` varchar(200) DEFAULT NULL,
  `home_team` varchar(200) DEFAULT NULL,
  `canonical_venue` varchar(300) DEFAULT NULL,
  `city` varchar(191) DEFAULT NULL,
  `country` varchar(191) DEFAULT NULL,
  `neutral_site` tinyint(1) DEFAULT NULL,
  `provider_venue` varchar(300) DEFAULT NULL,
  `map_geometry` json DEFAULT NULL,
  `map_source` varchar(191) DEFAULT NULL,
  `geometry_updated_at` datetime(6) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `ix_nfl_event_source_id` (`source_id`),
  UNIQUE KEY `ix_nfl_event_source_url` (`source_url`),
  KEY `ix_nfl_event_schedule_id` (`schedule_id`),
  KEY `ix_nfl_event_event_date` (`event_date`),
  KEY `ix_nfl_event_canonical_venue` (`canonical_venue`),
  KEY `ix_nfl_event_venue` (`venue`),
  KEY `ix_nfl_event_home_team` (`home_team`)
) ENGINE=InnoDB AUTO_INCREMENT=91 DEFAULT CHARSET=utf8mb3;

CREATE TABLE `nfl_iterations` (
  `id` int NOT NULL AUTO_INCREMENT,
  `event_id` int NOT NULL,
  `captured_at` datetime(6) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_nfl_event_capture_slot` (`event_id`,`captured_at`),
  KEY `ix_nfl_iterations_captured_at` (`captured_at`),
  KEY `ix_nfl_iterations_event_id` (`event_id`),
  CONSTRAINT `nfl_iterations_ibfk_1` FOREIGN KEY (`event_id`) REFERENCES `nfl_event` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=10370 DEFAULT CHARSET=utf8mb3;

CREATE TABLE `nfl_tickets` (
  `id` int NOT NULL AUTO_INCREMENT,
  `section` varchar(300) NOT NULL,
  `price` int NOT NULL,
  `listing_count` int NOT NULL,
  `iteration_id` int NOT NULL,
  PRIMARY KEY (`id`),
  KEY `ix_nfl_tickets_iteration_id` (`iteration_id`),
  KEY `ix_nfl_tickets_section` (`section`),
  CONSTRAINT `nfl_tickets_ibfk_1` FOREIGN KEY (`iteration_id`) REFERENCES `nfl_iterations` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=1562357 DEFAULT CHARSET=utf8mb3;

CREATE TABLE `section_bucket_summary` (
  `event_id` int NOT NULL,
  `section_key` varchar(600) NOT NULL,
  `bucket_slot` int NOT NULL,
  `section_name` varchar(300) NOT NULL,
  `median_price` float NOT NULL,
  `observation_count` int NOT NULL,
  `first_captured_at` datetime(6) NOT NULL,
  `last_captured_at` datetime(6) NOT NULL,
  `refreshed_at` datetime(6) NOT NULL,
  PRIMARY KEY (`event_id`,`section_key`,`bucket_slot`),
  KEY `ix_section_bucket_summary_event` (`event_id`),
  KEY `ix_section_bucket_summary_key_event` (`section_key`,`event_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3;

CREATE TABLE `section_summary_state` (
  `event_id` int NOT NULL AUTO_INCREMENT,
  `summary_version` int NOT NULL,
  `event_signature` varchar(64) NOT NULL,
  `source_iteration_id` int DEFAULT NULL,
  `source_iteration_count` int NOT NULL,
  `complete` tinyint(1) NOT NULL,
  `refreshed_at` datetime(6) NOT NULL,
  PRIMARY KEY (`event_id`)
) ENGINE=InnoDB AUTO_INCREMENT=91 DEFAULT CHARSET=utf8mb3;

-- sport: nhl

CREATE TABLE `analytics_dirty_venue` (
  `venue` varchar(300) NOT NULL,
  `revision` int NOT NULL,
  `dirty` tinyint(1) NOT NULL,
  `updated_at` datetime(6) NOT NULL,
  PRIMARY KEY (`venue`),
  KEY `ix_analytics_dirty_venue_dirty` (`dirty`,`updated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3;

CREATE TABLE `nhl_event` (
  `id` int NOT NULL AUTO_INCREMENT,
  `source_id` varchar(191) NOT NULL,
  `title` varchar(700) NOT NULL,
  `event_date` datetime(6) NOT NULL,
  `sections` json NOT NULL,
  `source_url` varchar(700) NOT NULL,
  `venue` varchar(300) NOT NULL,
  `schedule_id` varchar(191) DEFAULT NULL,
  `away_team` varchar(200) DEFAULT NULL,
  `home_team` varchar(200) DEFAULT NULL,
  `canonical_venue` varchar(300) DEFAULT NULL,
  `venue_timezone` varchar(255) DEFAULT NULL,
  `country` varchar(191) DEFAULT NULL,
  `neutral_site` tinyint(1) DEFAULT NULL,
  `game_type` int DEFAULT NULL,
  `season` int DEFAULT NULL,
  `currency` varchar(3) NOT NULL,
  `provider_venue` varchar(300) DEFAULT NULL,
  `map_geometry` json DEFAULT NULL,
  `map_source` varchar(191) DEFAULT NULL,
  `geometry_updated_at` datetime(6) DEFAULT NULL,
  `compacted_at` datetime(6) DEFAULT NULL,
  `original_iteration_count` int DEFAULT NULL,
  `retained_iteration_count` int DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `ix_nhl_event_source_url` (`source_url`),
  UNIQUE KEY `ix_nhl_event_source_id` (`source_id`),
  KEY `ix_nhl_event_canonical_venue` (`canonical_venue`),
  KEY `ix_nhl_event_venue` (`venue`),
  KEY `ix_nhl_event_home_team` (`home_team`),
  KEY `ix_nhl_event_schedule_id` (`schedule_id`),
  KEY `ix_nhl_event_event_date` (`event_date`)
) ENGINE=InnoDB AUTO_INCREMENT=204 DEFAULT CHARSET=utf8mb3;

CREATE TABLE `nhl_iterations` (
  `id` int NOT NULL AUTO_INCREMENT,
  `event_id` int NOT NULL,
  `captured_at` datetime(6) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_nhl_event_capture_slot` (`event_id`,`captured_at`),
  KEY `ix_nhl_iterations_event_id` (`event_id`),
  KEY `ix_nhl_iterations_captured_at` (`captured_at`),
  CONSTRAINT `nhl_iterations_ibfk_1` FOREIGN KEY (`event_id`) REFERENCES `nhl_event` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=1937 DEFAULT CHARSET=utf8mb3;

CREATE TABLE `nhl_tickets` (
  `id` int NOT NULL AUTO_INCREMENT,
  `section` varchar(300) NOT NULL,
  `price` int NOT NULL,
  `listing_count` int NOT NULL,
  `iteration_id` int NOT NULL,
  PRIMARY KEY (`id`),
  KEY `ix_nhl_tickets_section` (`section`),
  KEY `ix_nhl_tickets_iteration_id` (`iteration_id`),
  CONSTRAINT `nhl_tickets_ibfk_1` FOREIGN KEY (`iteration_id`) REFERENCES `nhl_iterations` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=143182 DEFAULT CHARSET=utf8mb3;

CREATE TABLE `section_bucket_summary` (
  `event_id` int NOT NULL,
  `section_key` varchar(600) NOT NULL,
  `bucket_slot` int NOT NULL,
  `section_name` varchar(300) NOT NULL,
  `median_price` float NOT NULL,
  `observation_count` int NOT NULL,
  `first_captured_at` datetime(6) NOT NULL,
  `last_captured_at` datetime(6) NOT NULL,
  `refreshed_at` datetime(6) NOT NULL,
  PRIMARY KEY (`event_id`,`section_key`,`bucket_slot`),
  KEY `ix_section_bucket_summary_key_event` (`section_key`,`event_id`),
  KEY `ix_section_bucket_summary_event` (`event_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb3;

CREATE TABLE `section_summary_state` (
  `event_id` int NOT NULL AUTO_INCREMENT,
  `summary_version` int NOT NULL,
  `event_signature` varchar(64) NOT NULL,
  `source_iteration_id` int DEFAULT NULL,
  `source_iteration_count` int NOT NULL,
  `complete` tinyint(1) NOT NULL,
  `refreshed_at` datetime(6) NOT NULL,
  PRIMARY KEY (`event_id`)
) ENGINE=InnoDB AUTO_INCREMENT=204 DEFAULT CHARSET=utf8mb3;
