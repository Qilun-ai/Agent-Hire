CREATE TABLE leads(
            job_id TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            url TEXT,
            platform TEXT,
            status TEXT DEFAULT 'discovered',
            score INTEGER DEFAULT 0,
            reason TEXT DEFAULT '',
            match_points TEXT DEFAULT '',
            asset_path TEXT DEFAULT '',
            cover_letter_path TEXT DEFAULT '',
            selected_projects TEXT DEFAULT '',
            description TEXT DEFAULT '',
            gaps TEXT DEFAULT '',
            resume_version INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        , kind TEXT DEFAULT 'job', budget TEXT DEFAULT '', signal_score INTEGER DEFAULT 0, signal_reason TEXT DEFAULT '', signal_tags TEXT DEFAULT '', outreach_reply TEXT DEFAULT '', outreach_dm TEXT DEFAULT '', source_meta TEXT DEFAULT '', outreach_email TEXT DEFAULT '', proposal_draft TEXT DEFAULT '', fit_bullets TEXT DEFAULT '', followup_sequence TEXT DEFAULT '', proof_snippet TEXT DEFAULT '', tech_stack TEXT DEFAULT '', location TEXT DEFAULT '', urgency TEXT DEFAULT '', base_signal_score INTEGER DEFAULT 0, feedback TEXT DEFAULT '', feedback_note TEXT DEFAULT '', followup_due_at TEXT DEFAULT '', last_contacted_at TEXT DEFAULT '', learning_delta INTEGER DEFAULT 0, learning_reason TEXT DEFAULT '', base_score INTEGER DEFAULT 0);
CREATE TABLE events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT,
            action TEXT,
            ts TEXT DEFAULT (datetime('now'))
        );
CREATE TABLE settings(
            key TEXT PRIMARY KEY,
            val TEXT
        );
CREATE TABLE error_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            error_type TEXT NOT NULL,
            error_message TEXT,
            source TEXT,
            count INTEGER DEFAULT 1,
            first_seen TEXT DEFAULT (datetime('now')),
            last_seen TEXT DEFAULT (datetime('now'))
        );
CREATE TABLE schema_migrations(
                name TEXT PRIMARY KEY,
                applied_at TEXT DEFAULT (datetime('now'))
            );
CREATE TABLE resume_templates(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source_filename TEXT DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idx_resume_templates_default ON resume_templates(is_default);
CREATE TABLE tags(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE documents(
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('resume','cover_letter')),
    topic TEXT NOT NULL DEFAULT '',
    tag_id TEXT REFERENCES tags(id) ON DELETE SET NULL,
    file_path TEXT NOT NULL,
    source_filename TEXT DEFAULT '',
    mime TEXT DEFAULT '',
    excerpt TEXT DEFAULT '',
    source TEXT DEFAULT 'upload',
    created_at TEXT DEFAULT (datetime('now'))
, resume_id TEXT DEFAULT '', parsed_profile_json TEXT DEFAULT '{}');
CREATE INDEX idx_documents_kind ON documents(kind);
CREATE INDEX idx_documents_tag ON documents(tag_id);
CREATE TABLE point_tags(
    point_kind TEXT NOT NULL CHECK(point_kind IN ('skill', 'project', 'experience')),
    point_id   TEXT NOT NULL,
    tag_id     TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (point_kind, point_id, tag_id)
);
CREATE INDEX idx_point_tags_tag ON point_tags(tag_id);
CREATE TABLE doc_selections(
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL DEFAULT '',
    tag_id TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    selection_json TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(job_id, tag_id)
);
CREATE INDEX idx_doc_sel_job ON doc_selections(job_id);
CREATE TABLE conflict_groups(
    id          TEXT PRIMARY KEY,
    reason      TEXT NOT NULL DEFAULT 'embedding'
                CHECK(reason IN ('exact', 'fuzzy', 'embedding', 'judge', 'manual')),
    score       REAL NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'open'
                CHECK(status IN ('open', 'picked', 'keep_both', 'dismissed')),
    picked_kind TEXT,
    picked_id   TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);
CREATE TABLE conflict_members(
    group_id   TEXT NOT NULL REFERENCES conflict_groups(id) ON DELETE CASCADE,
    point_kind TEXT NOT NULL CHECK(point_kind IN ('skill', 'project', 'experience')),
    point_id   TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (group_id, point_kind, point_id)
);
CREATE UNIQUE INDEX idx_conflict_member_point
    ON conflict_members(point_kind, point_id);
CREATE TABLE misc_context(
    id TEXT PRIMARY KEY CHECK(id = 'singleton'),
    merged_text TEXT NOT NULL DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE ingestion_tasks(
    task_id               TEXT PRIMARY KEY,
    status                TEXT NOT NULL DEFAULT 'processing'
                          CHECK(status IN ('processing', 'completed', 'failed', 'cancelled', 'review_required')),
    filename              TEXT NOT NULL DEFAULT '',
    file_path             TEXT NOT NULL DEFAULT '',
    tag_id                TEXT REFERENCES tags(id) ON DELETE SET NULL,
    topic                 TEXT NOT NULL DEFAULT '',
    stage                 TEXT NOT NULL DEFAULT 'reading'
                          CHECK(stage IN ('reading', 'extracting', 'deduping', 'indexing', 'completed', 'failed', 'idle')),
    stage_number          INTEGER NOT NULL DEFAULT 1,
    stage_message         TEXT NOT NULL DEFAULT '',
    progress_percent      INTEGER NOT NULL DEFAULT 0,
    thoughts_json         TEXT NOT NULL DEFAULT '[]',
    duplicates_json       TEXT NOT NULL DEFAULT '[]',
    result_json           TEXT NOT NULL DEFAULT '{}',
    error                 TEXT,
    started_at            TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at          TEXT,
    created_at            TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idx_ingestion_tasks_status ON ingestion_tasks(status);
CREATE INDEX idx_ingestion_tasks_started ON ingestion_tasks(started_at DESC);
CREATE INDEX idx_ingestion_tasks_tag ON ingestion_tasks(tag_id);
CREATE TABLE staged_duplicates(
    id                  TEXT PRIMARY KEY,
    task_id             TEXT NOT NULL REFERENCES ingestion_tasks(task_id) ON DELETE CASCADE,
    document_id         TEXT NOT NULL DEFAULT '',
    tag_id              TEXT DEFAULT NULL REFERENCES tags(id) ON DELETE SET NULL,
    parent_kind         TEXT NOT NULL CHECK(parent_kind IN ('experience', 'project')),
    parent_id           TEXT NOT NULL,
    company_name        TEXT NOT NULL DEFAULT '',
    role                TEXT NOT NULL DEFAULT '',
    period              TEXT NOT NULL DEFAULT '',
    entity_title        TEXT NOT NULL DEFAULT '',
    existing_point_id   TEXT NOT NULL,
    existing_text       TEXT NOT NULL,
    new_text            TEXT NOT NULL,
    explanation         TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'resolved_use_new', 'resolved_keep_both', 'resolved_keep_existing')),
    created_at          TEXT DEFAULT (datetime('now')),
    resolved_at         TEXT DEFAULT NULL
);
CREATE INDEX idx_staged_dup_task ON staged_duplicates(task_id);
CREATE INDEX idx_staged_dup_status ON staged_duplicates(status);
CREATE UNIQUE INDEX idx_documents_resume_id
    ON documents(resume_id) WHERE resume_id <> '';
CREATE TABLE discovery_save_sessions(
    session_id          TEXT PRIMARY KEY,
    result_set_id       TEXT NOT NULL,
    scope_hash          TEXT NOT NULL,
    acquisition_run_id  INTEGER,
    state               TEXT NOT NULL,
    request_snapshot    TEXT NOT NULL,
    total_count         INTEGER NOT NULL,
    next_after_ordinal  INTEGER DEFAULT -1,
    next_page_index     INTEGER NOT NULL DEFAULT 0,
    processed           INTEGER NOT NULL DEFAULT 0,
    saved               INTEGER NOT NULL DEFAULT 0,
    deduplicated        INTEGER NOT NULL DEFAULT 0,
    invalid             INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE TABLE discovery_save_batches(
    session_id          TEXT NOT NULL REFERENCES discovery_save_sessions(session_id) ON DELETE CASCADE,
    page_index          INTEGER NOT NULL,
    payload_hash        TEXT NOT NULL,
    receipt             TEXT NOT NULL,
    committed_at        TEXT NOT NULL,
    PRIMARY KEY(session_id, page_index)
);
CREATE TABLE discovery_save_memberships(
    session_id          TEXT NOT NULL REFERENCES discovery_save_sessions(session_id) ON DELETE CASCADE,
    result_ordinal      INTEGER NOT NULL,
    canonical_job_id    TEXT NOT NULL,
    normalized_url      TEXT NOT NULL,
    local_job_id        TEXT NOT NULL,
    outcome             TEXT NOT NULL,
    PRIMARY KEY(session_id, result_ordinal)
);
CREATE TABLE lead_identity_aliases(
    kind                TEXT NOT NULL,
    identity            TEXT NOT NULL,
    job_id              TEXT NOT NULL REFERENCES leads(job_id) ON DELETE CASCADE,
    PRIMARY KEY(kind, identity)
);
CREATE INDEX idx_save_sessions_resume
    ON discovery_save_sessions(scope_hash, state, updated_at);
CREATE INDEX idx_save_memberships_lead
    ON discovery_save_memberships(local_job_id);
CREATE INDEX idx_save_aliases_job
    ON lead_identity_aliases(job_id);
CREATE TABLE scraper_import_sessions(
    session_id         TEXT PRIMARY KEY,
    scraper_base_hash  TEXT NOT NULL,          -- sha256 of SCRAPER_API_BASE_URL (no secrets)
    run_ref            TEXT NOT NULL,
    corpus_generation  TEXT NOT NULL,
    snapshot_id        TEXT,
    view               TEXT NOT NULL,
    state              TEXT NOT NULL,          -- preparing|running|partial|failed|succeeded|stale
    next_cursor        TEXT,                   -- opaque, from the scraper
    next_page_index    INTEGER NOT NULL DEFAULT 0,
    total              INTEGER NOT NULL DEFAULT 0,
    processed          INTEGER NOT NULL DEFAULT 0,
    inserted           INTEGER NOT NULL DEFAULT 0,
    updated            INTEGER NOT NULL DEFAULT 0,
    unchanged          INTEGER NOT NULL DEFAULT 0,
    suppressed_deleted INTEGER NOT NULL DEFAULT 0,
    invalid            INTEGER NOT NULL DEFAULT 0,
    request_snapshot   TEXT NOT NULL,
    last_error         TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    attempt            INTEGER NOT NULL DEFAULT 1,
    UNIQUE(scraper_base_hash, corpus_generation, run_ref, view, attempt)
);
CREATE TABLE scraper_import_pages(
    session_id   TEXT NOT NULL REFERENCES scraper_import_sessions(session_id) ON DELETE CASCADE,
    page_index   INTEGER NOT NULL,
    cursor_in    TEXT NOT NULL DEFAULT '',
    payload_hash TEXT NOT NULL,
    receipt      TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    PRIMARY KEY(session_id, page_index)
);
CREATE TABLE scraper_import_memberships(
    session_id   TEXT NOT NULL REFERENCES scraper_import_sessions(session_id) ON DELETE CASCADE,
    ordinal      INTEGER NOT NULL,
    job_id       TEXT NOT NULL,     -- scraper canonical id
    local_job_id TEXT NOT NULL,
    outcome      TEXT NOT NULL,     -- inserted|updated|unchanged|suppressed_deleted|invalid
    filter_match TEXT NOT NULL,
    PRIMARY KEY(session_id, ordinal)
);
CREATE TABLE lead_job_data(
    job_id             TEXT PRIMARY KEY REFERENCES leads(job_id) ON DELETE CASCADE,
    scraper_job_id     TEXT NOT NULL,
    corpus_generation  TEXT NOT NULL,
    content_hash       TEXT NOT NULL,
    data_json          TEXT NOT NULL,   -- JobV1 minus enrichment/filter_match
    fetched_at         TEXT NOT NULL
);
CREATE TABLE lead_job_versions(
    job_id        TEXT NOT NULL REFERENCES leads(job_id) ON DELETE CASCADE,
    content_hash  TEXT NOT NULL,
    data_json     TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY(job_id, content_hash)
);
CREATE TABLE lead_enrichment(
    job_id               TEXT PRIMARY KEY REFERENCES leads(job_id) ON DELETE CASCADE,
    enrichment_revision  INTEGER NOT NULL,
    role_status          TEXT NOT NULL,
    primary_function_id  TEXT,
    taxonomy_version     TEXT,
    experience_level     TEXT,
    experience_status    TEXT NOT NULL,
    experience_reason    TEXT,
    min_required_years   INTEGER,
    max_required_years   INTEGER,
    preferred_min_years  INTEGER,
    requirements_status  TEXT NOT NULL,
    remote_policy        TEXT,
    result_json          TEXT NOT NULL,
    updated_at           TEXT NOT NULL
, sponsorship TEXT, employment_type TEXT, degree TEXT);
CREATE INDEX idx_lead_enrichment_exp ON lead_enrichment(experience_level, experience_status);
CREATE TABLE lead_role_memberships(
    job_id      TEXT NOT NULL REFERENCES leads(job_id) ON DELETE CASCADE,
    node_id     TEXT NOT NULL,
    membership  TEXT NOT NULL,
    PRIMARY KEY(job_id, node_id)
);
CREATE INDEX idx_lead_roles_node ON lead_role_memberships(node_id, membership);
CREATE TABLE lead_requirements(
    job_id       TEXT NOT NULL REFERENCES leads(job_id) ON DELETE CASCADE,
    item_id      TEXT NOT NULL,
    name         TEXT NOT NULL,
    item_type    TEXT NOT NULL,
    requirement  TEXT NOT NULL,
    alt_group    TEXT,
    PRIMARY KEY(job_id, item_id)
);
CREATE INDEX idx_lead_req_name ON lead_requirements(name, requirement);
CREATE TABLE lead_tombstones(
    kind        TEXT NOT NULL,      -- canonical|url
    identity    TEXT NOT NULL,      -- casefolded canonical id or normalized_source_url()
    deleted_at  TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT 'user_deleted',
    identities_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY(kind, identity)
);
CREATE TABLE job_feedback(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    scraper_job_id TEXT,
    signal TEXT NOT NULL CHECK(signal IN ('not_relevant','too_senior','too_junior','good')),
    created_at TEXT NOT NULL
);
CREATE INDEX idx_job_feedback_job ON job_feedback(job_id, created_at);
CREATE TABLE saved_searches(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    params_json TEXT NOT NULL,
    last_run_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE legacy_imports(
    import_id TEXT PRIMARY KEY,
    imported_at TEXT NOT NULL,
    counts_json TEXT NOT NULL
);
CREATE INDEX idx_job_feedback_signal_time ON job_feedback(signal, created_at);
CREATE INDEX idx_job_feedback_scraper_job ON job_feedback(scraper_job_id);
CREATE INDEX idx_lead_enrichment_sponsorship ON lead_enrichment(sponsorship);
CREATE INDEX idx_lead_req_job ON lead_requirements(job_id, requirement, name);
CREATE INDEX idx_leads_created_at ON leads(created_at);
CREATE INDEX idx_leads_status_kind ON leads(status, kind);
CREATE INDEX idx_leads_followup_due_at ON leads(followup_due_at);
CREATE INDEX idx_leads_feedback ON leads(feedback);
CREATE INDEX idx_events_job_id ON events(job_id);
CREATE INDEX idx_events_ts ON events(ts);
