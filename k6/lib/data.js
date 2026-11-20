import {
  SEED, A1_CASES, FALLBACK, PLAN_NAME, PRODUCT, CATEGORY, U2_SUMMARY, U3_NAMES, STATUS, STATUS_DIST,
  MAKISE_USER, MAKISE_PASSWORD,
} from './config.js';
import { rpc, login } from './jsonrpc.js';

// mulberry32: small seeded PRNG so runs are reproducible.
export function makeRng(seed) {
  let a = (seed >>> 0) || 1;
  return function () {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function pick(rng, arr) {
  return arr[Math.floor(rng() * arr.length)];
}

export function pickStatus(rng) {
  let r = rng();
  for (const [name, p] of STATUS_DIST) {
    if (r < p) return { name, id: STATUS[name] };
    r -= p;
  }
  return { name: 'PASSED', id: STATUS.PASSED };
}

export const u1Params = () => [{}];

export const u2Params = (planId) => [{
  category__product__name: PRODUCT,
  category__name: CATEGORY,
  plan: planId,
  summary__contains: U2_SUMMARY,
}];

export const u3Params = (name) => [{ summary: name }];

export function u3Name(rng) {
  return pick(rng, U3_NAMES);
}

const SETUP = { phase: 'setup', scenario: 'setup' };

function first(base, method, params, validate) {
  for (let i = 0; i < 3; i++) {
    const r = rpc(method, params, Object.assign({ op: 'setup' }, base), validate);
    if (r.ok) return r.result;
  }
  return null;
}

// Resolves ids by name and fixes the deterministic list of A1 case names.
export function resolveSetupData() {
  if (!login(MAKISE_USER, MAKISE_PASSWORD, SETUP)) {
    throw new Error('setup: makise login failed');
  }
  const isList = (v) => Array.isArray(v);

  const plans = first(SETUP, 'TestPlan.filter', [{ name: PLAN_NAME }], isList);
  const planId = plans && plans.length ? plans[0].id : FALLBACK.planId;
  if (!plans || !plans.length) console.warn(`setup: plan ${PLAN_NAME} not found by name, using id ${planId}`);

  const prio = (value, fallback) => {
    const r = first(SETUP, 'Priority.filter', [{ value__iexact: value }], isList);
    if (r && r.length) return r[0].id;
    console.warn(`setup: priority ${value} not resolved, using id ${fallback}`);
    return fallback;
  };

  const confirmed = first(SETUP, 'TestCaseStatus.filter', [{ name: 'CONFIRMED' }], isList);
  const builds = first(SETUP, 'Build.filter', [{}], isList);
  const cases = first(SETUP, 'TestCase.filter', [{ plan: planId }], isList);
  if (!cases || cases.length === 0) throw new Error('setup: no cases found in plan');
  if (!builds || builds.length === 0) throw new Error('setup: no build found');

  // Deterministic order (by id) and seeded shuffle, then take A1_CASES names.
  const names = cases.slice().sort((a, b) => a.id - b.id).map((c) => c.summary);
  const rng = makeRng(SEED);
  for (let i = names.length - 1; i > 0; i--) {
    const j = Math.floor(rng() * (i + 1));
    [names[i], names[j]] = [names[j], names[i]];
  }
  const build = builds.reduce((a, b) => (b.id > a.id ? b : a));

  return {
    planId,
    priorityMedium: prio('Medium', FALLBACK.priorityMedium),
    priorityHigh: prio('High', FALLBACK.priorityHigh),
    confirmedId: confirmed && confirmed.length ? confirmed[0].id : null,
    buildName: build.name,
    caseNames: names.slice(0, A1_CASES),
  };
}
