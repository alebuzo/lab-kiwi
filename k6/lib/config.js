const num = (name, def) => {
  const v = __ENV[name];
  return v === undefined || v === '' ? def : Number(v);
};

export const TEST_TYPE = __ENV.TEST_TYPE || 'smoke';
export const REP = __ENV.REP || '1';
export const BASE_URL = (__ENV.BASE_URL || 'https://kiwi-perf-https.kiwi.svc:8443').replace(/\/+$/, '');
export const RPC_URL = `${BASE_URL}/json-rpc/`;
export const MAKISE_USER = __ENV.MAKISE_USER || 'makise';
export const MAKISE_PASSWORD = __ENV.MAKISE_PASSWORD || '';
export const PERF_USER_PASSWORD = __ENV.PERF_USER_PASSWORD || '';
export const SEED = num('SEED', 20261119);
export const REQ_TIMEOUT = __ENV.REQ_TIMEOUT || '120s';

export const A1_CASES = num('A1_CASES', TEST_TYPE === 'smoke' ? 20 : 1500);
export const USER_VUS = num('VUS', { load: 6, endurance: 6, stress: 18, smoke: 1 }[TEST_TYPE] || 6);
export const MEASURE_S = num('MEASURE_S', { load: 3600, endurance: 14400, stress: 1800 }[TEST_TYPE]);
export const THINK_MIN_S = 1;
export const THINK_MAX_S = 3;
export const PROBE_PERIOD_S = num('PROBE_PERIOD_S', 10);

// Lenient lower bounds for response content validation.
export const U1_MIN = num('U1_MIN', 10000);
export const U2_MIN = num('U2_MIN', 1000);
export const U2_MAX = num('U2_MAX', 2000);

// Fallbacks used only when the id cannot be resolved by name in setup().
export const FALLBACK = {
  planId: num('PLAN_ID', 16),
  priorityMedium: num('PRIORITY_MEDIUM_ID', 16),
  priorityHigh: num('PRIORITY_HIGH_ID', 15),
};

export const PLAN_NAME = 'Regressions';
export const PRODUCT = 'PVSDK';
export const CATEGORY = 'Functional';
export const U2_SUMMARY = 'gibson';
export const U3_NAMES = [
  'test_gibson_mdio_cmd_list',
  'test_guild_tcp_fsm_eviction_timers',
  'test_tele_tunnel_rep_double_service_entry_config',
];

// Test Execution Status ids as defined in Kiwi (same as send_regressions_kiwi.py).
export const STATUS = { SKIPPED: 8, PASSED: 4, FAILED: 5, ERROR: 7 };
export const STATUS_DIST = [
  ['PASSED', 0.916],
  ['FAILED', 0.055],
  ['SKIPPED', 0.021],
  ['ERROR', 0.009],
];

export function userName(vuIndex, total) {
  const i = ((vuIndex - 1) % total) + 1;
  return `perf_user_${String(i).padStart(2, '0')}`;
}

// Shared layout for load / endurance / stress. All durations in seconds.
export function standardScenarios({ a1PeriodS, a1PreAlloc, a1MaxVUs }) {
  const warmupS = num('WARMUP_S', 300);
  const cooldownS = num('COOLDOWN_S', 300);
  const nVus = USER_VUS;
  const measure = MEASURE_S;
  const period = num('A1_PERIOD_S', a1PeriodS);
  return {
    users_warmup: {
      executor: 'ramping-vus',
      exec: 'usersWarmup',
      startVUs: 0,
      stages: [{ duration: `${warmupS}s`, target: nVus }],
      gracefulRampDown: '60s',
      gracefulStop: '60s',
    },
    users: {
      executor: 'constant-vus',
      exec: 'usersMeasure',
      vus: nVus,
      duration: `${measure}s`,
      startTime: `${warmupS}s`,
      gracefulStop: '60s',
    },
    automation: {
      executor: 'constant-arrival-rate',
      exec: 'automation',
      rate: 1,
      timeUnit: `${period}s`,
      duration: `${measure}s`,
      startTime: `${warmupS}s`,
      preAllocatedVUs: a1PreAlloc,
      maxVUs: a1MaxVUs,
      gracefulStop: '15m',
    },
    recovery_probe: {
      executor: 'constant-vus',
      exec: 'recoveryProbe',
      vus: 1,
      duration: `${cooldownS}s`,
      startTime: `${warmupS + measure}s`,
      gracefulStop: '60s',
    },
  };
}
