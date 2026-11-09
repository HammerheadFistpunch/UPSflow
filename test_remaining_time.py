"""Run the dashboard's actual time formatting code against River 2 readings."""
import ast
from pathlib import Path
import shutil
import subprocess

import pytest


def test_dashboard_remaining_time_uses_minutes():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is required to exercise dashboard JavaScript')
    source = ast.parse(Path(__file__).with_name('upsflow.py').read_text(encoding='utf-8'))
    dashboard = next(ast.literal_eval(item.value) for item in source.body
                     if isinstance(item, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id == 'DASHBOARD_HTML'
                             for target in item.targets))
    # Execute the real dashboard helpers without starting its browser polling loop.
    script = dashboard.split('<script>', 1)[1].split('let __upsflowRefreshBusy', 1)[0]
    script += r'''
const assert = require('node:assert/strict');
assert.equal(formatRemainingTime(76), '01:16');
assert.equal(formatRemainingTime(1), '00:01');
assert.equal(formatRemainingTime(125), '02:05');
const row = (charging, discharging, net) => remainingTimeRow({raw_telemetry:{
  remaining_time_charging:charging, remaining_time_discharging:discharging
}}, net);
assert.match(row(76, 40, 200), /Time to charge.*\+01:16/);
assert.match(row(76, 125, -200), /Time to discharge.*-02:05/);
for (const invalid of [null, undefined, 0, -1, 5999, 65535, NaN]) {
  assert.match(row(invalid, invalid, 200), /Time remaining.*—/);
  assert.match(row(invalid, invalid, -200), /Time remaining.*—/);
}
assert.match(row(76, 125, 0), /Time remaining.*—/);
assert.equal(rawLabels.remaining_time_charging, 'Remaining charging time (min)');
assert.equal(rawLabels.remaining_time_discharging, 'Remaining discharging time (min)');
'''
    result = subprocess.run([node, '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
