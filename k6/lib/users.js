import { sleep } from 'k6';
import exec from 'k6/execution';
import {
  PERF_USER_PASSWORD, THINK_MIN_S, THINK_MAX_S, PROBE_PERIOD_S, U1_MIN, U2_MIN, U2_MAX, USER_VUS, SEED,
} from './config.js';
import { userName } from './config.js';
import { rpc, login } from './jsonrpc.js';
import { makeRng, u1Params, u2Params, u3Params, u3Name } from './data.js';

let rng = null;
let loggedIn = false;
let nVus = 1;

function state(total) {
  if (!rng) rng = makeRng(SEED + exec.vu.idInTest * 7919);
  nVus = total;
}

function ensureLogin(base, idx) {
  if (loggedIn) return true;
  loggedIn = login(userName(idx, nVus), PERF_USER_PASSWORD, base);
  return loggedIn;
}

function think() {
  sleep(THINK_MIN_S + rng() * (THINK_MAX_S - THINK_MIN_S));
}

const isLen = (min, max) => (v) => Array.isArray(v) && v.length >= min && v.length <= max;

function runIteration(data, phase, userCount) {
  state(userCount);
  const scenario = exec.scenario.name;
  const base = { phase, scenario };
  if (!ensureLogin(base, exec.vu.idInTest)) {
    sleep(5);
    return;
  }

  rpc('TestCase.filter', u1Params(), Object.assign({ op: 'U1' }, base), isLen(U1_MIN, Infinity));
  think();

  rpc('TestCase.filter', u2Params(data.planId), Object.assign({ op: 'U2' }, base), isLen(U2_MIN, U2_MAX));
  think();

  const u3 = rpc('TestCase.filter', u3Params(u3Name(rng)), Object.assign({ op: 'U3' }, base), isLen(1, 2));
  think();

  if (u3.ok) {
    // TestCase.update falla en Kiwi 13.5.5-sdk al serializar la respuesta (Platform); U4 alterna un tag
    const method = exec.scenario.iterationInTest % 2 === 0 ? 'TestCase.add_tag' : 'TestCase.remove_tag';
    rpc(method, [u3.result[0].id, 'perf-u4'], Object.assign({ op: 'U4' }, base), () => true);
  }
  think();
}

export function usersWarmup(data) {
  runIteration(data, 'warmup', USER_VUS);
}

export function usersMeasure(data) {
  runIteration(data, 'measure', USER_VUS);
}

export function recoveryProbe(data) {
  state(1);
  const t0 = Date.now();
  const base = { phase: 'cooldown', scenario: 'recovery_probe' };
  if (!loggedIn) loggedIn = login(userName(1, 18), PERF_USER_PASSWORD, base);
  if (loggedIn) {
    rpc('TestCase.filter', u3Params(u3Name(rng)), Object.assign({ op: 'U3' }, base), isLen(1, 2));
  }
  sleep(Math.max(0, PROBE_PERIOD_S - (Date.now() - t0) / 1000));
}
