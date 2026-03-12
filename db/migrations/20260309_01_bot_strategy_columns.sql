-- 20260309_01_bot_strategy_columns.sql
-- Purpose: move strategy ownership to public."Bot" and optionally remove legacy timeframe columns.
-- Safe to run multiple times.

BEGIN;

ALTER TABLE public."Bot"
ADD COLUMN IF NOT EXISTS "strategy" text;

ALTER TABLE public."Bot"
ADD COLUMN IF NOT EXISTS "strategyParams" jsonb NOT NULL DEFAULT '{}'::jsonb;

UPDATE public."Bot" b
SET
  "strategy" = COALESCE(b."strategy", cfg."strategy", 'peak_dip'),
  "strategyParams" = COALESCE(b."strategyParams", cfg."params", '{}'::jsonb)
FROM public."BotStrategyConfig" cfg
WHERE cfg."botId" = b."id"
  AND (b."strategy" IS NULL OR b."strategyParams" IS NULL);

UPDATE public."Bot"
SET "strategy" = 'peak_dip'
WHERE "strategy" IS NULL;

COMMIT;

-- Optional cleanup (run only after confirming no other service uses these columns):
-- ALTER TABLE public."Bot" DROP COLUMN IF EXISTS "trendTimeframe";
-- ALTER TABLE public."Bot" DROP COLUMN IF EXISTS "signalTimeframe";
