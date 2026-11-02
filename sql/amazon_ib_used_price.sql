-- Snapshot of Active Amazon AU-IB-<upc>-OB / -UE / -UG (used sku) prices, replaced hourly by
-- rvpartstore (sources.save_amazon_ib_prices); read by IBS order placement (eBay used follows Amazon used).
-- Separate from amazon_ib_price on purpose: that table is keyed by upc and read as the NEW price.
-- report_time = mtime of the Amazon report file, machine-local clock.
CREATE TABLE IF NOT EXISTS inventory.amazon_ib_used_price (
    upc         VARCHAR(20)   NOT NULL,
    grade       CHAR(2)       NOT NULL,  -- 'OB' | 'UE' | 'UG'
    sku         VARCHAR(40)   NOT NULL,
    price       DECIMAL(10,2) NOT NULL,
    report_time DATETIME      NOT NULL,
    PRIMARY KEY (upc, grade)
);
