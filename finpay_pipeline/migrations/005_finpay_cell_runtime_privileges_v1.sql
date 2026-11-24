/* Least-privilege runtime grants for cell reporting. Apply only after 003/004. */
BEGIN;

DO $$
BEGIN
    IF current_database() <> 'finpay' THEN
        RAISE EXCEPTION 'cell runtime privilege grant requires the finpay database';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'finpay') THEN
        RAISE EXCEPTION 'required finpay runtime role is missing';
    END IF;
    IF to_regclass('public.finpay_cell_configs_v1') IS NULL
       OR to_regclass('public.finpay_cell_sources_v1') IS NULL
       OR to_regclass('public.finpay_cell_ledger_v1') IS NULL
       OR to_regclass('public.finpay_cell_calculations_v1') IS NULL
       OR to_regclass('public.finpay_cell_current_v1') IS NULL
       OR to_regclass('public.finpay_cell_current_results_v1') IS NULL THEN
        RAISE EXCEPTION 'required cell relations are missing';
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO finpay;
GRANT SELECT, INSERT ON TABLE public.finpay_cell_configs_v1 TO finpay;
GRANT SELECT, INSERT ON TABLE public.finpay_cell_sources_v1 TO finpay;
GRANT SELECT, INSERT ON TABLE public.finpay_cell_ledger_v1 TO finpay;
GRANT SELECT, INSERT ON TABLE public.finpay_cell_calculations_v1 TO finpay;
GRANT SELECT, INSERT, UPDATE ON TABLE public.finpay_cell_current_v1 TO finpay;
GRANT SELECT ON TABLE public.finpay_cell_current_results_v1 TO finpay;

COMMIT;
