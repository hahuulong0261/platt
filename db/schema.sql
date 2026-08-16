PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('supervisor', 'monitored'))
);

CREATE TABLE IF NOT EXISTS cameras (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS supervisor_cameras (
    supervisor_id INTEGER NOT NULL,
    camera_id INTEGER NOT NULL,
    PRIMARY KEY (supervisor_id, camera_id),
    FOREIGN KEY (supervisor_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (camera_id) REFERENCES cameras(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS supervisor_monitored (
    supervisor_id INTEGER NOT NULL,
    monitored_id INTEGER NOT NULL,
    PRIMARY KEY (supervisor_id, monitored_id),
    FOREIGN KEY (supervisor_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (monitored_id) REFERENCES users(id) ON DELETE CASCADE,
    CHECK (supervisor_id <> monitored_id)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_event_id TEXT UNIQUE,
    camera_id INTEGER NOT NULL,
    type TEXT NOT NULL DEFAULT 'fall' CHECK (type = 'fall'),
    started_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    ended_at TEXT,
    signal_count INTEGER NOT NULL DEFAULT 1 CHECK (signal_count > 0),
    max_confidence REAL NOT NULL CHECK (
        max_confidence >= 0 AND max_confidence <= 1
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (camera_id) REFERENCES cameras(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS event_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL,
    image_path TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    position INTEGER NOT NULL CHECK (position BETWEEN 1 AND 10),
    UNIQUE (event_id, position),
    FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS notifications (
    event_id INTEGER NOT NULL,
    supervisor_id INTEGER NOT NULL,
    read_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (event_id, supervisor_id),
    FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE,
    FOREIGN KEY (supervisor_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_events_camera_open
    ON events(camera_id, type, ended_at);

CREATE INDEX IF NOT EXISTS idx_notifications_supervisor_unread
    ON notifications(supervisor_id, read_at);
