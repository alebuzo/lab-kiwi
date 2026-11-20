import exec from 'k6/execution';
import { MEASURE_S, MAKISE_USER, MAKISE_PASSWORD, PRODUCT, REP, TEST_TYPE } from './config.js';
import { rpc, login, a1RunDuration, a1RunsStarted, a1RunsCompleted } from './jsonrpc.js';
import { makeRng, pickStatus } from './data.js';
import { SEED } from './config.js';

const isList = (v) => Array.isArray(v);
const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
const hasId = (v) => isObj(v) && v.id !== undefined;
const now = () => new Date().toISOString().replace('T', ' ').slice(0, 19);

export function automation(data) {
  // constant-arrival-rate may fire one extra iteration exactly at the end of the window; never start A1 in cooldown.
  if (MEASURE_S && (Date.now() - exec.scenario.startTime) / 1000 >= MEASURE_S - 1) return;
  const n = exec.scenario.iterationInTest + 1;
  const runId = `${REP}-${n}`;
  const base = { phase: 'measure', scenario: 'automation', run_id: runId };
  const runTags = { run_id: runId, test_type: TEST_TYPE, rep: String(REP) };
  const t0 = Date.now();
  let failed = 0;
  let aborted = false;

  a1RunsStarted.add(1, runTags);

  const call = (method, params, validate) => {
    const r = rpc(method, params, Object.assign({ op: `A1_${method}` }, base), validate);
    if (!r.ok) failed++;
    return r;
  };

  if (!login(MAKISE_USER, MAKISE_PASSWORD, base)) {
    failed++;
    aborted = true;
  }

  let runPk = null;
  if (!aborted) {
    // Backend.configure() equivalents
    call('Product.filter', [{ name: PRODUCT }], isList);
    call('Version.filter', [{ product__name: PRODUCT }], isList);
    call('Category.filter', [{ product__name: PRODUCT }], isList);
    call('Priority.filter', [{}], isList);
    call('TestCaseStatus.filter', [{}], isList);
    call('TestExecutionStatus.filter', [{}], isList);

    const build = call('Build.filter', [{ name: data.buildName }], (v) => isList(v) && v.length > 0);
    call('TestRun.filter', [{ summary__contains: `Test Run for ${data.buildName}, job ${runId}` }], isList);
    const summary = `Test Run for ${data.buildName}, job ${runId}`;
    const run = build.ok
      ? call('TestRun.create', [{
        summary, plan: data.planId, build: build.result[0].id, manager: MAKISE_USER, start_date: now(),
      }], hasId)
      : { ok: false };
    if (run.ok) runPk = run.result.id;
    else aborted = true;
  }

  if (!aborted) {
    const rng = makeRng(SEED + n * 104729);
    const startDate = now();
    for (const name of data.caseNames) {
      const found = call('TestCase.filter', [{ summary: name, category__product__name: PRODUCT }],
        (v) => isList(v) && v.length > 0);
      if (!found.ok) continue;
      const tc = found.result[0];

      const inPlan = call('TestCase.filter', [{ pk: tc.id, plan: data.planId }], isList);
      if (inPlan.ok && inPlan.result.length === 0) call('TestPlan.add_case', [data.planId, tc.id], isList);

      if (data.confirmedId !== null && tc.case_status !== data.confirmedId) {
        call('TestCase.update', [tc.id, { case_status: data.confirmedId }], isObj);
      }

      const added = call('TestRun.add_case', [runPk, tc.id], (v) => isList(v) && v.length > 0 && hasId(v[0]));
      if (!added.ok) continue;

      for (const ex of added.result) {
        const st = pickStatus(rng);
        call('TestExecution.update', [ex.id, { status: st.id, start_date: startDate }], isObj);
        if (st.name === 'FAILED' || st.name === 'ERROR') {
          call('TestExecution.add_comment', [ex.id, `Test case failed or errored: \`${name}\``], (v) => v !== undefined);
        }
      }
    }
    call('TestRun.update', [runPk, { stop_date: now() }], isObj);
  }

  const status = failed === 0 ? 'ok' : 'error';
  a1RunDuration.add(Date.now() - t0, {
    run_id: runId, phase: 'measure', status, calls_failed: String(failed), test_type: TEST_TYPE, rep: String(REP),
  });
  a1RunsCompleted.add(1, { run_id: runId, status, test_type: TEST_TYPE, rep: String(REP) });
}
