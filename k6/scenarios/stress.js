import { standardScenarios } from '../lib/config.js';

export function scenarios() {
  return standardScenarios({ a1PeriodS: 200, a1PreAlloc: 4, a1MaxVUs: 12 });
}
