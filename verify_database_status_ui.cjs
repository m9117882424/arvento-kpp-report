#!/usr/bin/env node
// Exercise the actual JavaScript injected by the Python portal entrypoint.
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const vm = require('node:vm');

const source = JSON.parse(execFileSync('python3', ['-c', `
import ast,json
from pathlib import Path
tree=ast.parse(Path('run_report_portal.py').read_text())
for node in ast.walk(tree):
    if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='replace' and len(node.args)==2:
        try:
            before=ast.literal_eval(node.args[0])
            after=ast.literal_eval(node.args[1])
        except (ValueError,TypeError):
            continue
        if before=='loadDates();':
            start=after.index('async function loadDatabaseStatus()')
            end=after.index("dateInput.addEventListener('change', updateDatabaseCompleteness);",start)
            print(json.dumps(after[start:end]))
            break
else:
    raise SystemExit('Database status JavaScript not found')
`], {cwd: __dirname, encoding: 'utf8'}));

async function check(response, success, expectedText) {
  const context = {
    fetch: async () => response,
    databaseStatusData: {latest_date: '2026-11-15'},
    dbStatus: {}, dbStatusTime: {}, dbStatusMeta: {}, dbCompleteness: {},
    ageText: () => '5 мин назад',
    updateDatabaseCompleteness: () => {},
  };
  vm.createContext(context);
  await vm.runInContext(source + '\nloadDatabaseStatus();', context);
  if (success) {
    assert.equal(context.dbStatus.className, 'db-status fresh');
    assert.equal(context.dbStatusTime.textContent, '16.11.2026 11:00:00');
    assert.equal(context.databaseStatusData.status, 'fresh');
  } else {
    assert.equal(context.dbStatus.className, 'db-status error');
    assert.equal(context.databaseStatusData, null);
    assert.equal(context.dbCompleteness.textContent, '');
    assert.match(context.dbStatusMeta.textContent, expectedText);
    assert.doesNotMatch(context.dbStatusMeta.textContent, /Unexpected token|Internal Server Error/);
  }
}

(async () => {
  const body = {status: 'fresh', latest_display: '16.11.2026 11:00:00',
    age_minutes: 5, today_points: 123, today_vehicles: 7, label: 'Данные актуальны'};
  await check({ok: true, status: 200, json: async () => body}, true);
  await check({ok: false, status: 503, json: async () => ({detail: 'База временно недоступна'})}, false, /База временно/);
  for (const status of [200, 500, 502]) {
    await check({ok: status === 200, status,
      json: async () => { throw new SyntaxError('Unexpected token I, Internal Server Error'); }},
      false, /Повторная попытка через минуту/);
  }
  await check({ok: true, status: 200, json: async () => null}, false, /временно недоступна/);
  console.log('OK: database status displays JSON errors, non-JSON responses and valid data safely');
})().catch(error => { console.error(error); process.exitCode = 1; });
