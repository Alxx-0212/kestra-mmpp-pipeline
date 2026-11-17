-- Run only after reviewing and backing up the exact FinPay database.
BEGIN;

DO $$
BEGIN
    IF current_database() <> 'finpay' THEN
        RAISE EXCEPTION 'cell publication-state removal requires the finpay database';
    END IF;
END
$$;

DROP TABLE IF EXISTS public.finpay_cell_manual_resets_v1;
DROP TABLE IF EXISTS public.finpay_cell_publication_dates_v1;
DROP TABLE IF EXISTS public.finpay_cell_publications_v1;
DROP TABLE IF EXISTS public.finpay_cell_target_operations_v1;
DROP TABLE IF EXISTS public.finpay_cell_dirty_dates_v1;

COMMIT;
