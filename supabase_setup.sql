-- ═══════════════════════════════════════════════════════════════════════════
-- DMARC Pipeline — Supabase table setup
-- Run this in: Supabase Dashboard → SQL Editor → New Query
-- ═══════════════════════════════════════════════════════════════════════════

-- ── Reports table ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dmarc_reports (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    xml_filename VARCHAR(255) NOT NULL,
    archive_filename VARCHAR(255),
    org_name VARCHAR(255),
    org_email VARCHAR(255),
    report_id VARCHAR(128) UNIQUE,
    date_begin TIMESTAMPTZ,
    date_end TIMESTAMPTZ,
    domain VARCHAR(255),
    adkim VARCHAR(8),
    aspf VARCHAR(8),
    p VARCHAR(16),
    sp VARCHAR(16),
    pct INTEGER DEFAULT 100,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── Records table ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dmarc_records (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_id BIGINT REFERENCES dmarc_reports(id) ON DELETE CASCADE,
    source_ip VARCHAR(45),
    count INTEGER DEFAULT 0,
    header_from VARCHAR(255),
    envelope_from VARCHAR(255),
    envelope_to VARCHAR(255),
    disposition VARCHAR(16),
    dkim_aligned BOOLEAN DEFAULT FALSE,
    spf_aligned BOOLEAN DEFAULT FALSE,
    dkim_result VARCHAR(16),
    spf_result VARCHAR(16),
    dkim_domain VARCHAR(255),
    spf_domain VARCHAR(255),
    dkim_auth_json JSONB DEFAULT '[]'::jsonb,
    spf_auth_json JSONB DEFAULT '[]'::jsonb
);

-- ── Gmail accounts table (OAuth) ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gmail_accounts (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    credentials_json JSONB DEFAULT '{}'::jsonb,
    token_json JSONB DEFAULT '{}'::jsonb,
    is_active BOOLEAN DEFAULT TRUE,
    last_sync TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── Outlook accounts table (Microsoft Graph API OAuth) ──────────────────────
CREATE TABLE IF NOT EXISTS outlook_accounts (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    credentials_json JSONB DEFAULT '{}'::jsonb,
    token_json JSONB DEFAULT '{}'::jsonb,
    is_active BOOLEAN DEFAULT TRUE,
    last_sync TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── Processed emails table (track what we've checked) ────────────────────────
CREATE TABLE IF NOT EXISTS processed_emails (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id BIGINT REFERENCES gmail_accounts(id) ON DELETE CASCADE,
    message_id VARCHAR(255) NOT NULL,
    processed_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(account_id, message_id)
);

-- ── Indexes ──────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_reports_domain ON dmarc_reports(domain);
CREATE INDEX IF NOT EXISTS idx_reports_date_end ON dmarc_reports(date_end DESC);
CREATE INDEX IF NOT EXISTS idx_records_report_id ON dmarc_records(report_id);
CREATE INDEX IF NOT EXISTS idx_records_source_ip ON dmarc_records(source_ip);
CREATE INDEX IF NOT EXISTS idx_gmail_accounts_email ON gmail_accounts(email);
CREATE INDEX IF NOT EXISTS idx_gmail_accounts_active ON gmail_accounts(is_active);
CREATE INDEX IF NOT EXISTS idx_outlook_accounts_email ON outlook_accounts(email);
CREATE INDEX IF NOT EXISTS idx_outlook_accounts_active ON outlook_accounts(is_active);
CREATE INDEX IF NOT EXISTS idx_processed_emails_lookup ON processed_emails(account_id, message_id);
