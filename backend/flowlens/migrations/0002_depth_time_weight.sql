-- v2: book frames carry the sampled time (ms) so frames sampled at different
-- periods (live 1 s, DEMO fast-forward 5 s) aggregate with correct time weights.
ALTER TABLE depth_1m ADD COLUMN time_ms INTEGER;
