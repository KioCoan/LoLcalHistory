-- Local match history. Personal data; never leaves this machine.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS matches (
    game_id           INTEGER NOT NULL,
    platform_id       TEXT    NOT NULL DEFAULT '',
    queue_id          INTEGER,
    queue_name        TEXT,
    game_mode         TEXT,
    game_type         TEXT,
    map_id            INTEGER,
    game_creation_ms  INTEGER,
    game_duration_s   INTEGER,
    game_version      TEXT,
    winning_team_id   INTEGER,
    source            TEXT    NOT NULL,
    source_rank       INTEGER NOT NULL DEFAULT 0,
    captured_at       TEXT    NOT NULL,
    raw_path          TEXT,
    -- Your own rank movement for this game, filled in shortly after it ends.
    my_rank_queue     TEXT,
    my_lp_delta       INTEGER,
    my_lp_after       INTEGER,
    my_tier_after     TEXT,
    my_division_after TEXT,
    PRIMARY KEY (game_id, platform_id)
);

CREATE INDEX IF NOT EXISTS idx_matches_creation ON matches (game_creation_ms DESC);
CREATE INDEX IF NOT EXISTS idx_matches_queue ON matches (queue_id);

CREATE TABLE IF NOT EXISTS participants (
    game_id             INTEGER NOT NULL,
    platform_id         TEXT    NOT NULL DEFAULT '',
    participant_id      INTEGER NOT NULL,
    puuid               TEXT,
    riot_id_game_name   TEXT,
    riot_id_tagline     TEXT,
    summoner_name       TEXT,
    team_id             INTEGER,
    champion_id         INTEGER,
    champion_name       TEXT,
    position            TEXT,
    win                 INTEGER,
    placement           INTEGER,
    kills               INTEGER,
    deaths              INTEGER,
    assists             INTEGER,
    cs                  INTEGER,
    gold_earned         INTEGER,
    damage_to_champions INTEGER,
    damage_taken        INTEGER,
    vision_score        INTEGER,
    champ_level         INTEGER,
    item0 INTEGER, item1 INTEGER, item2 INTEGER, item3 INTEGER,
    item4 INTEGER, item5 INTEGER, item6 INTEGER,
    spell1_id           INTEGER,
    spell2_id           INTEGER,
    PRIMARY KEY (game_id, platform_id, participant_id),
    FOREIGN KEY (game_id, platform_id) REFERENCES matches (game_id, platform_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_participants_puuid ON participants (puuid);
CREATE INDEX IF NOT EXISTS idx_participants_champion ON participants (champion_id);

-- Separate table rather than augment1..augmentN columns: the number of augments
-- varies by mode and has changed across patches, and a wide table would need a
-- migration every time Riot adds a slot.
CREATE TABLE IF NOT EXISTS participant_augments (
    game_id        INTEGER NOT NULL,
    platform_id    TEXT    NOT NULL DEFAULT '',
    participant_id INTEGER NOT NULL,
    slot           INTEGER NOT NULL,
    augment_id     INTEGER,
    augment_name   TEXT,
    PRIMARY KEY (game_id, platform_id, participant_id, slot),
    FOREIGN KEY (game_id, platform_id) REFERENCES matches (game_id, platform_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_augments_id ON participant_augments (augment_id);

-- puuid is the stable identity. Riot IDs get changed; names are display only.
CREATE TABLE IF NOT EXISTS players (
    puuid             TEXT PRIMARY KEY,
    riot_id_game_name TEXT,
    riot_id_tagline   TEXT,
    summoner_name     TEXT,
    first_seen        TEXT,
    last_seen         TEXT
);

-- Which puuids are you. Multiple rows are allowed so a second account works.
CREATE TABLE IF NOT EXISTS me (
    puuid             TEXT PRIMARY KEY,
    riot_id_game_name TEXT,
    riot_id_tagline   TEXT,
    updated_at        TEXT
);

-- Latest known rank per player and queue. Refreshed opportunistically; the
-- timestamp matters because a rank read today is not the rank someone held
-- during a match played last week.
CREATE TABLE IF NOT EXISTS player_ranks (
    puuid         TEXT    NOT NULL,
    queue_type    TEXT    NOT NULL,
    tier          TEXT,
    division      TEXT,
    league_points INTEGER,
    wins          INTEGER,
    losses        INTEGER,
    updated_at    TEXT,
    PRIMARY KEY (puuid, queue_type)
);

-- Rank as it stood when a specific game was captured. Only the watcher can
-- populate this, because the client only ever reports current rank — a
-- backfilled game has no way to recover what someone was ranked at the time.
CREATE TABLE IF NOT EXISTS participant_ranks (
    game_id        INTEGER NOT NULL,
    platform_id    TEXT    NOT NULL DEFAULT '',
    participant_id INTEGER NOT NULL,
    queue_type     TEXT    NOT NULL,
    tier           TEXT,
    division       TEXT,
    league_points  INTEGER,
    PRIMARY KEY (game_id, platform_id, participant_id, queue_type),
    FOREIGN KEY (game_id, platform_id) REFERENCES matches (game_id, platform_id) ON DELETE CASCADE
);

-- Your own rank over time, one row per observation. The LP change for a match
-- is derived by differencing consecutive observations.
CREATE TABLE IF NOT EXISTS rank_progress (
    taken_at      TEXT    NOT NULL,
    queue_type    TEXT    NOT NULL,
    tier          TEXT,
    division      TEXT,
    league_points INTEGER,
    wins          INTEGER,
    losses        INTEGER,
    PRIMARY KEY (taken_at, queue_type)
);

DROP VIEW IF EXISTS v_my_matches;
CREATE VIEW v_my_matches AS
SELECT
    m.game_id, m.platform_id, m.queue_id, m.queue_name, m.game_mode, m.game_type,
    m.map_id, m.game_creation_ms, m.game_duration_s, m.source,
    m.my_rank_queue, m.my_lp_delta, m.my_lp_after, m.my_tier_after, m.my_division_after,
    p.participant_id, p.puuid, p.team_id, p.champion_id, p.champion_name,
    p.position, p.win, p.placement, p.kills, p.deaths, p.assists, p.cs,
    p.gold_earned, p.damage_to_champions, p.damage_taken, p.vision_score,
    p.champ_level, p.spell1_id, p.spell2_id
FROM matches m
JOIN participants p
  ON p.game_id = m.game_id AND p.platform_id = m.platform_id
JOIN me ON me.puuid = p.puuid;

DROP VIEW IF EXISTS v_champion_stats;
CREATE VIEW v_champion_stats AS
SELECT
    champion_id,
    COALESCE(champion_name, 'Champion ' || champion_id) AS champion_name,
    COUNT(*)                                   AS games,
    SUM(COALESCE(win, 0))                      AS wins,
    COUNT(*) - SUM(COALESCE(win, 0))           AS losses,
    ROUND(100.0 * SUM(COALESCE(win, 0)) / COUNT(*), 1) AS win_rate,
    ROUND(AVG(kills), 1)                       AS avg_kills,
    ROUND(AVG(deaths), 1)                      AS avg_deaths,
    ROUND(AVG(assists), 1)                     AS avg_assists,
    ROUND(AVG(cs), 1)                          AS avg_cs,
    ROUND(AVG(damage_to_champions))            AS avg_damage,
    ROUND(AVG(placement), 2)                   AS avg_placement
FROM v_my_matches
GROUP BY champion_id;

DROP VIEW IF EXISTS v_augment_stats;
CREATE VIEW v_augment_stats AS
SELECT
    a.augment_id,
    COALESCE(a.augment_name, 'Augment ' || a.augment_id) AS augment_name,
    COUNT(*)                                   AS games,
    SUM(COALESCE(v.win, 0))                    AS wins,
    ROUND(100.0 * SUM(COALESCE(v.win, 0)) / COUNT(*), 1) AS win_rate,
    ROUND(AVG(v.kills), 1)                     AS avg_kills,
    ROUND(AVG(v.deaths), 1)                    AS avg_deaths,
    ROUND(AVG(v.assists), 1)                   AS avg_assists,
    ROUND(AVG(v.placement), 2)                 AS avg_placement
FROM v_my_matches v
JOIN participant_augments a
  ON  a.game_id        = v.game_id
  AND a.platform_id    = v.platform_id
  AND a.participant_id = v.participant_id
GROUP BY a.augment_id;

DROP VIEW IF EXISTS v_teammates;
CREATE VIEW v_teammates AS
SELECT
    t.puuid,
    COALESCE(pl.riot_id_game_name, t.summoner_name, substr(t.puuid, 1, 8)) AS name,
    pl.riot_id_tagline                         AS tagline,
    COUNT(*)                                   AS games,
    SUM(COALESCE(t.win, 0))                    AS wins,
    COUNT(*) - SUM(COALESCE(t.win, 0))         AS losses,
    ROUND(100.0 * SUM(COALESCE(t.win, 0)) / COUNT(*), 1) AS win_rate,
    MAX(v.game_creation_ms)                    AS last_played_ms
FROM v_my_matches v
JOIN participants t
  ON  t.game_id     = v.game_id
  AND t.platform_id = v.platform_id
  AND t.team_id     = v.team_id
LEFT JOIN players pl ON pl.puuid = t.puuid
WHERE t.puuid IS NOT NULL
  AND t.puuid NOT IN (SELECT puuid FROM me)
GROUP BY t.puuid;
