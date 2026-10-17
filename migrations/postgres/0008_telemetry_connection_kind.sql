-- A project's own "app log push" source: the monitored application sends its structured events to TRACE (signed webhook).
-- It is a connection of kind 'telemetry' so it does not collide with the hosting connection (Vercel) in readiness.
ALTER TABLE connections DROP CONSTRAINT connections_kind_check;
ALTER TABLE connections ADD CONSTRAINT connections_kind_check CHECK (kind IN ('code','hosting','database','telemetry'));
