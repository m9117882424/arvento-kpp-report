-- Roster attributes are overlaid at export time. The ad-hoc 2026-10-09
-- trigger invalidated complete GPS calculations on every roster replacement.
-- Freshness checks now verify actual row counts and effective-roster coverage
-- as well as heavy-source watermarks, so a newly listed vehicle still causes
-- a rebuild. Existing STALE days remain stale until a verified recalculation.
DO $migration$
BEGIN
    IF to_regclass('public.consolidated_roster_snapshots') IS NOT NULL THEN
        DROP TRIGGER IF EXISTS trg_roster_invalidates_consolidated_cache
            ON public.consolidated_roster_snapshots;
    END IF;
END
$migration$;

DROP FUNCTION IF EXISTS public.invalidate_consolidated_cache_on_roster_change();
