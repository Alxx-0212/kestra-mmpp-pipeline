ALTER TABLE linkaja_monthly_refreshes
    ADD COLUMN google_status TEXT NOT NULL DEFAULT 'NOT_ATTEMPTED';

ALTER TABLE linkaja_monthly_refreshes
    ADD COLUMN google_error TEXT;
