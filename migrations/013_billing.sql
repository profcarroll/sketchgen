-- 013_billing.sql — what Oracle has metered, day by day, so cost can be tracked
-- rather than spot-checked.
--
-- Until now the bill lived in four `meta` keys: one amount, one currency, one
-- window, one timestamp. That is a snapshot, and a snapshot cannot answer the
-- only questions worth asking — is it going up, when did it start, and which
-- service is responsible. The console card could say "$0.00" and nothing about
-- whether that had been true for a day or a month.
--
-- One row per (tenancy, UTC day, service, SKU). The grain is Oracle's own: the
-- Usage API returns exactly this shape at DAILY granularity, so a row here is a
-- row there and nothing is derived on the way in. Re-reading a day overwrites
-- it, because Oracle restates the last day or two as meters settle, and the
-- later reading is the better one.
--
-- `sku` is skuPartNumber (B93297, B93298, ...) and not skuName, because the
-- part number is the stable identifier and the name is marketing text Oracle
-- has already changed once. The name is kept alongside for the card to show.
--
-- `quantity` matters as much as `amount` and sometimes more. An Always Free
-- tenancy is charged nothing, so `amount` is 0.00 every day and stays 0.00
-- right up until it doesn't; `quantity` is the number that moves first. 96
-- OCPU-hours in a day is four OCPUs pinned for twenty-four hours, and that is
-- readable long before a dollar appears.
--
-- **`tenancy` is in the primary key on purpose.** On 2026-09-18 the operator's
-- ~/.oci/config was found to point at a different tenancy from the one the node
-- runs in, and every figure the card had ever shown — including the $0.00
-- recorded against "every service, from 1 August" — was read from an unrelated
-- account. Nothing in the old four keys could have caught that: an amount is an
-- amount. Keying on the tenancy means two accounts' readings sit side by side
-- and are visibly two things, and the card can refuse to add them up.

CREATE TABLE IF NOT EXISTS billing_usage (
    tenancy     TEXT NOT NULL,           -- the tenancy OCID the row was read from
    day         TEXT NOT NULL,           -- 'YYYY-MM-DD', UTC, the day usage started
    service     TEXT NOT NULL,           -- 'Compute', 'Block Storage', ...
    sku         TEXT NOT NULL,           -- skuPartNumber, the stable id
    sku_name    TEXT,                    -- 'Standard - A1 - Memory', for the card
    quantity    REAL NOT NULL DEFAULT 0, -- metered amount, in `unit`
    unit        TEXT,                    -- 'OCPU Per Hour', 'Gigabyte Per Hour', ...
    amount      REAL NOT NULL DEFAULT 0, -- what it was charged, before credits
    currency    TEXT,                    -- 'USD', or NULL on rows Oracle leaves blank
    fetched_utc TEXT NOT NULL,           -- when this reading was taken
    PRIMARY KEY (tenancy, day, service, sku)
);

-- The card asks for a month of one tenancy at a time, newest first.
CREATE INDEX IF NOT EXISTS idx_billing_usage_day
    ON billing_usage (tenancy, day DESC);
