export function scenarios() {
  return {
    users: {
      executor: 'per-vu-iterations',
      exec: 'usersMeasure',
      vus: 1,
      iterations: 1,
      maxDuration: '90s',
    },
    automation: {
      executor: 'per-vu-iterations',
      exec: 'automation',
      vus: 1,
      iterations: 1,
      startTime: '3s',
      maxDuration: '90s',
    },
    recovery_probe: {
      executor: 'constant-vus',
      exec: 'recoveryProbe',
      vus: 1,
      duration: '20s',
      startTime: '25s',
      gracefulStop: '30s',
    },
  };
}
