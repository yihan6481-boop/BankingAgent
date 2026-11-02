-- Service protection has no user data. Runtime can only call fixed functions;
-- it cannot inspect, reset, or directly mutate these tables.
CREATE TABLE ops.login_limits (
 key TEXT PRIMARY KEY CHECK(length(key)=64), attempts INTEGER NOT NULL,
 window_at TIMESTAMPTZ NOT NULL, locked_until TIMESTAMPTZ
);
CREATE FUNCTION pg02.login_gate(k TEXT, mode TEXT, at_time TIMESTAMPTZ,
 threshold_value INTEGER, window_seconds INTEGER, cooldown_seconds INTEGER)
RETURNS INTEGER LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE r ops.login_limits; remaining INTEGER;
BEGIN
 IF length(k)<>64 OR mode NOT IN ('acquire','success','status','release') OR
 threshold_value NOT BETWEEN 1 AND 1000 OR window_seconds NOT BETWEEN 1 AND 86400
 OR cooldown_seconds NOT BETWEEN 1 AND 3600 THEN RAISE EXCEPTION 'invalid protection arguments'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('login:'||k,0));
 DELETE FROM ops.login_limits WHERE window_at < at_time - interval '2 days'
 AND (locked_until IS NULL OR locked_until <= at_time);
 SELECT * INTO r FROM ops.login_limits WHERE key=k FOR UPDATE;
 IF FOUND AND r.locked_until>at_time THEN
  RETURN ceil(extract(epoch FROM r.locked_until-at_time))::INTEGER;
 END IF;
 IF mode='success' THEN DELETE FROM ops.login_limits WHERE key=k; RETURN 0; END IF;
 IF mode='release' THEN
  UPDATE ops.login_limits SET attempts=greatest(0,attempts-1) WHERE key=k;
  RETURN 0;
 END IF;
 IF mode='status' THEN RETURN 0; END IF;
 IF r.key IS NULL OR r.window_at + make_interval(secs=>window_seconds)<=at_time
 OR r.locked_until IS NOT NULL THEN
  INSERT INTO ops.login_limits VALUES(k,1,at_time,NULL)
  ON CONFLICT(key) DO UPDATE SET attempts=1,window_at=at_time,locked_until=NULL;
 ELSE
  UPDATE ops.login_limits SET attempts=attempts+1 WHERE key=k;
 END IF;
 SELECT * INTO r FROM ops.login_limits WHERE key=k;
 -- Reserve before authentication so parallel requests cannot all pass a check.
 IF r.attempts>threshold_value THEN
  UPDATE ops.login_limits SET locked_until=at_time+make_interval(secs=>cooldown_seconds) WHERE key=k;
  RETURN cooldown_seconds;
 END IF;
 RETURN 0;
END; $$;

CREATE TABLE ops.tool_breakers (
 dependency TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0,
 window_at TIMESTAMPTZ NOT NULL, open_until TIMESTAMPTZ,
 probe_until TIMESTAMPTZ, last_error TEXT
);
CREATE FUNCTION pg02.tool_breaker(k TEXT, mode TEXT, at_time TIMESTAMPTZ,
 threshold_value INTEGER, window_seconds INTEGER, cooldown_seconds INTEGER, error_value TEXT DEFAULT NULL)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE r ops.tool_breakers; allowed BOOLEAN:=false; state_value TEXT;
BEGIN
 IF k NOT IN ('bank_storage','payment_storage','recall_service') OR
 mode NOT IN ('read','write','success','failure','status','neutral') OR
 threshold_value NOT BETWEEN 1 AND 100 OR window_seconds NOT BETWEEN 1 AND 86400 OR
 cooldown_seconds NOT BETWEEN 1 AND 3600 THEN RAISE EXCEPTION 'invalid protection arguments'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('breaker:'||k,0));
 INSERT INTO ops.tool_breakers(dependency,window_at) VALUES(k,at_time) ON CONFLICT DO NOTHING;
 SELECT * INTO r FROM ops.tool_breakers WHERE dependency=k FOR UPDATE;
 IF mode='success' THEN
  UPDATE ops.tool_breakers SET failures=0,window_at=at_time,open_until=NULL,probe_until=NULL,last_error=NULL WHERE dependency=k;
 ELSIF mode='failure' THEN
  IF r.open_until IS NULL AND r.window_at+make_interval(secs=>window_seconds)<=at_time THEN r.failures:=0; r.window_at:=at_time; END IF;
  r.failures:=r.failures+1;
  UPDATE ops.tool_breakers SET failures=r.failures,window_at=r.window_at,last_error=left(error_value,80),probe_until=NULL,
   open_until=CASE WHEN r.open_until IS NOT NULL OR r.failures>=threshold_value
    THEN at_time+make_interval(secs=>cooldown_seconds) ELSE NULL END WHERE dependency=k;
 ELSIF mode='neutral' THEN
  -- A refusal proves no dependency health; release a probe with finite cooldown.
  IF r.probe_until IS NOT NULL THEN
   UPDATE ops.tool_breakers SET probe_until=NULL,open_until=at_time+make_interval(secs=>cooldown_seconds) WHERE dependency=k;
  END IF;
 ELSIF mode IN ('read','write') THEN
  IF r.open_until IS NULL THEN allowed:=true;
  ELSIF mode='read' AND r.open_until<=at_time AND (r.probe_until IS NULL OR r.probe_until<=at_time) THEN
   allowed:=true;
   UPDATE ops.tool_breakers SET probe_until=at_time+make_interval(secs=>cooldown_seconds) WHERE dependency=k;
  END IF;
 END IF;
 SELECT * INTO r FROM ops.tool_breakers WHERE dependency=k;
 state_value:=CASE WHEN r.open_until IS NULL THEN 'closed'
   WHEN r.open_until>at_time THEN 'open' ELSE 'half_open' END;
 RETURN jsonb_build_object('allowed',allowed,'state',state_value,'failure_count',r.failures,
  'retry_after_seconds',CASE WHEN r.open_until IS NULL THEN 0 ELSE greatest(1,ceil(extract(epoch FROM
    greatest(r.open_until,coalesce(r.probe_until,r.open_until))-at_time))::INTEGER) END);
END; $$;
REVOKE ALL ON ops.login_limits,ops.tool_breakers FROM PUBLIC,banking_runtime;
REVOKE ALL ON FUNCTION pg02.login_gate(TEXT,TEXT,TIMESTAMPTZ,INTEGER,INTEGER,INTEGER) FROM PUBLIC;
REVOKE ALL ON FUNCTION pg02.tool_breaker(TEXT,TEXT,TIMESTAMPTZ,INTEGER,INTEGER,INTEGER,TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION pg02.login_gate(TEXT,TEXT,TIMESTAMPTZ,INTEGER,INTEGER,INTEGER) TO banking_runtime;
GRANT EXECUTE ON FUNCTION pg02.tool_breaker(TEXT,TEXT,TIMESTAMPTZ,INTEGER,INTEGER,INTEGER,TEXT) TO banking_runtime;
