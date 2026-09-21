-- ============================================================
-- Baseball Charting App — Draft Schema (v0.1, MySQL)
-- Designed for use with MySQL Workbench
-- ============================================================
-- NOTES:
--   - This is a first draft based on the project plan, before
--     seeing real charts. Field names/enums for pitch_type,
--     result codes, etc. will likely need to match your actual
--     notation once we review a sample chart.
--   - "source_charts" exists to support Step 1b/1c: every
--     uploaded photo is tracked, along with the raw LLM
--     extraction output, so nothing is lost and extraction
--     can be reviewed/re-run without re-uploading.
--   - All tables use InnoDB (required for foreign key support).
-- ============================================================

CREATE DATABASE IF NOT EXISTS baseball_charting
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

USE baseball_charting;

-- ------------------------------------------------------------
-- PLAYERS
-- One row per person. A player can be a pitcher, hitter, or
-- both (e.g. a two-way player, or just because your team
-- tracks everyone the same way).
-- ------------------------------------------------------------
CREATE TABLE players (
    player_id       INT AUTO_INCREMENT PRIMARY KEY,
    first_name      VARCHAR(100) NOT NULL,
    last_name       VARCHAR(100) NOT NULL,
    role            ENUM('pitcher', 'hitter', 'both') NOT NULL,
    bats            ENUM('L', 'R', 'S'),          -- S = switch
    throws          ENUM('L', 'R'),
    team            VARCHAR(100),
    level           VARCHAR(50),                   -- e.g. 'Varsity', 'JV', 'College'
    notes           TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_players_name (last_name, first_name)
) ENGINE=InnoDB;

-- ------------------------------------------------------------
-- GAMES
-- ------------------------------------------------------------
CREATE TABLE games (
    game_id         INT AUTO_INCREMENT PRIMARY KEY,
    game_date       DATE NOT NULL,
    season          VARCHAR(20),                   -- e.g. '2024', '2024 Fall'
    opponent        VARCHAR(100),
    location        VARCHAR(150),
    home_away       ENUM('home', 'away', 'neutral'),
    notes           TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_games_date (game_date)
) ENGINE=InnoDB;

-- ------------------------------------------------------------
-- SOURCE_CHARTS
-- Tracks every uploaded chart image and its extraction status.
-- Both at_bats and pitches can optionally point back to the
-- chart image they were extracted from, for review/audit.
-- ------------------------------------------------------------
CREATE TABLE source_charts (
    chart_id             INT AUTO_INCREMENT PRIMARY KEY,
    game_id              INT,
    chart_type           ENUM('pitching', 'hitting') NOT NULL,
    image_path           VARCHAR(500) NOT NULL,     -- file path or storage URL
    uploaded_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
    extraction_status    ENUM('pending', 'extracted', 'reviewed', 'error') NOT NULL DEFAULT 'pending',
    raw_extraction_json  JSON,                      -- unedited LLM output, kept for audit/debugging
    reviewed_by          VARCHAR(100),
    reviewed_at          DATETIME,
    notes                TEXT,

    FOREIGN KEY (game_id) REFERENCES games(game_id),
    INDEX idx_source_charts_status (extraction_status)
) ENGINE=InnoDB;

-- ------------------------------------------------------------
-- AT_BATS
-- One row per plate appearance.
-- ------------------------------------------------------------
CREATE TABLE at_bats (
    at_bat_id       INT AUTO_INCREMENT PRIMARY KEY,
    game_id         INT NOT NULL,
    pitcher_id      INT NOT NULL,
    hitter_id       INT NOT NULL,
    source_chart_id INT,

    inning          TINYINT,
    inning_half     ENUM('top', 'bottom'),
    outs_before     TINYINT CHECK (outs_before BETWEEN 0 AND 2),

    -- Final outcome of the at-bat
    result          VARCHAR(20) NOT NULL,   -- e.g. 'K', 'BB', 'HBP', '1B', '2B', '3B', 'HR',
                                             -- 'GO' (groundout), 'FO' (flyout), 'LO', 'DP', 'FC', 'ROE'
    rbi             TINYINT DEFAULT 0,

    -- If ball was put in play: location on the field, normalized 0-1
    -- (0,0 = home plate corner of a bounding box; adjust convention
    -- once we see your spray chart layout)
    hit_location_x  DECIMAL(5,4),
    hit_location_y  DECIMAL(5,4),
    hit_type        ENUM('ground_ball', 'line_drive', 'fly_ball', 'popup'),

    final_balls     TINYINT,
    final_strikes   TINYINT,

    notes           TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (game_id) REFERENCES games(game_id),
    FOREIGN KEY (pitcher_id) REFERENCES players(player_id),
    FOREIGN KEY (hitter_id) REFERENCES players(player_id),
    FOREIGN KEY (source_chart_id) REFERENCES source_charts(chart_id),

    INDEX idx_atbats_pitcher (pitcher_id),
    INDEX idx_atbats_hitter (hitter_id),
    INDEX idx_atbats_game (game_id),
    INDEX idx_atbats_result (result)
) ENGINE=InnoDB;

-- ------------------------------------------------------------
-- PITCHES
-- One row per pitch thrown within an at-bat. This is the most
-- granular table and where most of the "insights" queries will
-- draw from.
-- ------------------------------------------------------------
CREATE TABLE pitches (
    pitch_id        INT AUTO_INCREMENT PRIMARY KEY,
    at_bat_id       INT NOT NULL,
    sequence_number TINYINT NOT NULL,       -- 1st pitch, 2nd pitch, etc. within the AB

    pitch_type      VARCHAR(20),            -- e.g. 'FB', 'CB', 'SL', 'CH', 'CT', 'SI' — will
                                             -- likely be replaced with your actual shorthand
    velocity        DECIMAL(4,1),           -- mph

    -- Location in/around the strike zone, normalized coordinates.
    -- Convention: 0-1 spans the zone itself; values <0 or >1 are
    -- outside the zone in that direction. Keeps raw precision
    -- rather than bucketing into a 9-zone grid at storage time.
    location_x      DECIMAL(5,4),
    location_y      DECIMAL(5,4),

    balls_before    TINYINT CHECK (balls_before BETWEEN 0 AND 3),
    strikes_before  TINYINT CHECK (strikes_before BETWEEN 0 AND 2),

    result          VARCHAR(30) NOT NULL,   -- e.g. 'called_strike', 'swinging_strike', 'ball',
                                             -- 'foul', 'in_play', 'hit_by_pitch'

    notes           TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (at_bat_id) REFERENCES at_bats(at_bat_id),
    UNIQUE KEY uq_atbat_sequence (at_bat_id, sequence_number),
    INDEX idx_pitches_atbat (at_bat_id),
    INDEX idx_pitches_type (pitch_type),
    INDEX idx_pitches_result (result)
) ENGINE=InnoDB;

-- ============================================================
-- OPEN QUESTIONS to resolve once we see a real chart:
--   1. What's the exact shorthand for pitch types on your charts?
--   2. How is pitch location marked — a dot in a drawn zone,
--      a numbered grid, coordinates? This affects how confidently
--      we can extract location_x/location_y vs. a coarser zone.
--   3. What result codes/abbreviations do you use for at-bat
--      outcomes and pitch-by-pitch results?
--   4. Do your charts track baserunners? Not modeled yet —
--      easy to add a `runners_on` field or separate table if needed.
--   5. Is velocity always recorded, or only sometimes?
-- ============================================================
