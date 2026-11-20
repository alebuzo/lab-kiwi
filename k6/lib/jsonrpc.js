import http from 'k6/http';
import { Trend, Rate, Counter } from 'k6/metrics';
import { RPC_URL, BASE_URL, REQ_TIMEOUT, TEST_TYPE, REP } from './config.js';

export const durMetrics = {
  U1: new Trend('dur_U1', true),
  U2: new Trend('dur_U2', true),
  U3: new Trend('dur_U3', true),
  U4: new Trend('dur_U4', true),
};
export const durA1Call = new Trend('dur_A1_call', true);
export const a1RunDuration = new Trend('a1_run_duration', true);
export const rpcOk = new Rate('rpc_ok');
export const a1RunsStarted = new Counter('a1_runs_started');
export const a1RunsCompleted = new Counter('a1_runs_completed');

let nextId = 1;
let logged = 0;

function classify(res) {
  if (res.error_code === 1050) return 'timeout';
  if (res.status !== 200) return `http_${res.status}`;
  return null;
}

// Performs one JSON-RPC call, validates the content and records metrics.
// base = { op, phase, scenario, run_id? } ; validate(result) -> bool
export function rpc(method, params, base, validate) {
  const tags = Object.assign({ test_type: TEST_TYPE, rep: String(REP) }, base);
  const body = JSON.stringify({ jsonrpc: '2.0', id: nextId++, method, params });
  const res = http.post(RPC_URL, body, {
    headers: { 'Content-Type': 'application/json', Referer: `${BASE_URL}/` },
    timeout: REQ_TIMEOUT,
    tags,
  });

  let errorType = classify(res);
  let result;
  if (!errorType) {
    let parsed;
    try {
      parsed = JSON.parse(res.body);
    } catch (e) {
      errorType = 'body_invalid';
    }
    if (!errorType) {
      if (parsed === null || typeof parsed !== 'object') errorType = 'body_invalid';
      else if (parsed.error !== undefined && parsed.error !== null) errorType = 'rpc_error';
      else if (!('result' in parsed)) errorType = 'body_invalid';
      else {
        result = parsed.result;
        if (validate && !validate(result)) errorType = 'body_invalid';
      }
    }
  }

  const ok = errorType === null;
  if (!ok && logged++ < 20) {
    console.warn(`${method} failed: ${errorType} status=${res.status} body=${String(res.body).slice(0, 200)}`);
  }
  const mTags = Object.assign({}, tags);
  if (!ok) mTags.error_type = errorType;
  rpcOk.add(ok, mTags);
  const dur = res.timings.duration;
  if (base.op in durMetrics) durMetrics[base.op].add(dur, tags);
  else if (base.op.startsWith('A1_')) durA1Call.add(dur, tags);
  return { ok, result, errorType, res };
}

export function login(username, password, base) {
  const r = rpc('Auth.login', [username, password], Object.assign({}, base, { op: 'login' }),
    (v) => typeof v === 'string' && v.length > 0);
  // Make sure the session cookie is present even if the server only returns the id in the body.
  if (r.ok) http.cookieJar().set(BASE_URL, 'sessionid', r.result);
  return r.ok;
}
