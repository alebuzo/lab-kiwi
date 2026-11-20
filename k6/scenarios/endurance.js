import { standardScenarios } from '../lib/config.js';

export function scenarios() {
  return standardScenarios({ a1PeriodS: 600, a1PreAlloc: 3, a1MaxVUs: 6 });
}
