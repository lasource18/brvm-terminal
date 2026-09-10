-- PR-AB (part 2): the job-missed watchdog.
--
-- `job_runs` is the audit trail every scheduled job writes through
-- `services/watchdog.tracked`: one row at start (status 'running'),
-- updated at finish. The watchdog derives each job's due times from its
-- own APScheduler trigger and compares them against this table, so a job
-- that silently never fired — process restarted across its cron minute,
-- executor wedged, scheduler thread dead — shows up as a gap instead of
-- a missing log line nobody reads.
--
-- `job_problems` is the set of problems currently open, keyed
-- '<job_id>:<kind>'. A row exists while the problem persists: the
-- watchdog notifies once when it appears, again every
-- OPS_ALERT_REPEAT_HOURS, and once more when it clears. Without this the
-- 15-minute check would repeat the same alert 96 times a day.

CREATE TABLE IF NOT EXISTS job_runs (
    job_id       TEXT NOT NULL,
    started_utc  TEXT NOT NULL,
    finished_utc TEXT,
    status       TEXT NOT NULL DEFAULT 'running',   -- running | ok | skipped | failed
    note         TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (job_id, started_utc)
);

CREATE INDEX IF NOT EXISTS idx_job_runs_started ON job_runs(started_utc);

CREATE TABLE IF NOT EXISTS job_problems (
    key               TEXT NOT NULL PRIMARY KEY,    -- '<job_id>:<kind>'
    job_id            TEXT NOT NULL,
    kind              TEXT NOT NULL,                -- missed | failed | stuck
    note              TEXT NOT NULL DEFAULT '',
    first_seen_utc    TEXT NOT NULL,
    last_notified_utc TEXT NOT NULL
);
