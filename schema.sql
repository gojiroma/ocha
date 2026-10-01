-- Unique table names for the dictionary + mutterings app
-- Dictionary entries table
CREATE TABLE IF NOT EXISTS ouch_dict_entries (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    reading TEXT,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ouch_dict_entries_title ON ouch_dict_entries (title);
CREATE INDEX IF NOT EXISTS idx_ouch_dict_entries_reading ON ouch_dict_entries (reading);
CREATE INDEX IF NOT EXISTS idx_ouch_dict_entries_updated_at ON ouch_dict_entries (updated_at);

-- Dictionary entry history for versioning
CREATE TABLE IF NOT EXISTS ouch_dict_entries_history (
    id SERIAL PRIMARY KEY,
    entry_id INTEGER NOT NULL REFERENCES ouch_dict_entries(id),
    title TEXT NOT NULL,
    reading TEXT,
    content TEXT NOT NULL,
    archived_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ouch_dict_entries_history_entry_id ON ouch_dict_entries_history (entry_id);
CREATE INDEX IF NOT EXISTS idx_ouch_dict_entries_history_archived_at ON ouch_dict_entries_history (archived_at);

-- Mutterings/timeline entries
CREATE TABLE IF NOT EXISTS ouch_mutterings (
    id SERIAL PRIMARY KEY,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ouch_mutterings_created_at ON ouch_mutterings (created_at);

-- Sync state for LocalStorage + Neon synchronization
CREATE TABLE IF NOT EXISTS ouch_sync_state (
    sync_id TEXT PRIMARY KEY,
    ciphertext TEXT NOT NULL,
    iv TEXT NOT NULL,
    content_updated_at TIMESTAMPTZ NOT NULL,
    last_synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ouch_sync_state_last_synced_at ON ouch_sync_state (last_synced_at);

-- Export flags for tracking
CREATE TABLE IF NOT EXISTS ouch_export_flags (
    sync_id TEXT PRIMARY KEY,
    last_export_date TEXT NOT NULL,
    last_synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ouch_export_flags_last_synced_at ON ouch_export_flags (last_synced_at);
