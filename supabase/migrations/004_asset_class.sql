-- Asset class column for stock vs crypto API routing.

ALTER TABLE strategies
    ADD COLUMN IF NOT EXISTS asset_class TEXT NOT NULL DEFAULT 'stock';

UPDATE strategies
SET asset_class = 'crypto'
WHERE symbol LIKE '%/%'
   OR name = 'momentum_btc';
