-- Market data index. The bars themselves live in Parquet files, one per
-- instrument, timeframe and month; these tables say what is where.
--
-- `store` names the place: 'library' for data kept on this machine, and later
-- 'scratch:<app session id>' for data held only for one app session.

CREATE TABLE data_partitions (
    user_id         TEXT NOT NULL DEFAULT 'local',
    store           TEXT NOT NULL,
    instrument_key  TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    month           TEXT NOT NULL,
    rows            INTEGER NOT NULL,
    first_bar       TEXT NOT NULL,
    last_bar        TEXT NOT NULL,
    bytes           INTEGER NOT NULL,
    source          TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (user_id, store, instrument_key, timeframe, month)
);

-- Trading sessions the broker had no bars for (a holiday the calendar does not
-- know, a date before listing). Remembered so they are not requested again.
CREATE TABLE data_no_data (
    user_id         TEXT NOT NULL DEFAULT 'local',
    instrument_key  TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    session_date    TEXT NOT NULL,
    checked_at      TEXT NOT NULL,
    PRIMARY KEY (user_id, instrument_key, timeframe, session_date)
);
