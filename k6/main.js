import { TEST_TYPE, BASE_URL, REP } from './lib/config.js';
import { resolveSetupData } from './lib/data.js';
import * as load from './scenarios/load.js';
import * as endurance from './scenarios/endurance.js';
import * as stress from './scenarios/stress.js';
import * as smoke from './scenarios/smoke.js';

export { usersWarmup, usersMeasure, recoveryProbe } from './lib/users.js';
export { automation } from './lib/a1.js';

const defs = { load, endurance, stress, smoke };
if (!defs[TEST_TYPE]) {
  throw new Error(`TEST_TYPE must be one of ${Object.keys(defs).join('|')}, got '${TEST_TYPE}'`);
}

export const options = {
  scenarios: defs[TEST_TYPE].scenarios(),
  insecureSkipTLSVerify: true,
  noCookiesReset: true,
  tags: { test_type: TEST_TYPE, rep: String(REP) },
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)'],
  thresholds: {
    dur_U1: [{ threshold: 'med<8000', abortOnFail: false }],
    dur_U2: [{ threshold: 'med<8000', abortOnFail: false }],
    dur_U3: [{ threshold: 'med<8000', abortOnFail: false }],
    dur_U4: [{ threshold: 'med<8000', abortOnFail: false }],
  },
};

export function setup() {
  console.log(`test_type=${TEST_TYPE} rep=${REP} base_url=${BASE_URL}`);
  return resolveSetupData();
}
