"""Bounded reporter tests through actual Node; no installs, network or staging."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPORTER = ROOT / 'scripts/modular_ci_vitest_reporter.mjs'
NODE = shutil.which('node')
pytestmark = pytest.mark.skipif(NODE is None, reason='Node runtime unavailable')


def run_node(tmp_path, body, *, expected=0):
    script = """
import * as api from REPORTER;
import * as fs from 'node:fs';
import path from 'node:path';
const directory = process.argv[2];
const testCase = (state, name='fixture', errors=[]) => ({fullName:name, result:()=>({state,errors})});
const module = (tests, state='passed', errors=[]) => ({state:()=>state, errors:()=>errors,
  children:{*allTests(){yield* tests;}}});
""".replace('REPORTER', json.dumps(REPORTER.as_uri())) + body
    env = dict(os.environ)
    env.pop('EH_CI_VITEST_REPORT_DIR', None)
    result = subprocess.run([NODE, '--input-type=module', '-', str(tmp_path)], input=script,
        text=True, capture_output=True, timeout=30, env=env)
    assert result.returncode == expected, result.stdout + result.stderr
    return result


def summary(tmp_path):
    report = json.loads((tmp_path / 'ui-summary.json').read_text(encoding='utf-8'))
    assert (tmp_path / 'ui-summary.json').stat().st_size <= 64 * 1024
    detail = json.loads((tmp_path / 'ui-detail.json').read_text(encoding='utf-8'))
    assert (tmp_path / 'ui-detail.json').stat().st_size <= 512 * 1024
    assert report['details']['bytes'] == (tmp_path / 'ui-detail.json').stat().st_size
    return report, detail


def test_actual_node_counts_skipped_and_todo_without_failing_success(tmp_path):
    run_node(tmp_path, """
const reporter = new api.BoundedVitestReporter({directory});
reporter.onTestRunStart([{}]);
reporter.onTestRunEnd([module([testCase('passed'),testCase('skipped'),testCase('skipped')])], [], 'passed');
""")
    report, _ = summary(tmp_path)
    assert report['complete'] and report['state'] == 'passed'
    assert report['outcomes'] == dict(passed=1, failed=0, skipped=2, pending=0, unknown=0, total=3)


@pytest.mark.parametrize('state,reason', [('pending','passed'),('unknown','passed'),('passed','interrupted')])
def test_pending_unknown_or_interrupted_is_not_complete_success(tmp_path, state, reason):
    run_node(tmp_path, f"""
const reporter = new api.BoundedVitestReporter({{directory}});
reporter.onTestRunStart([{{}}]);
reporter.onTestRunEnd([module([testCase({json.dumps(state)})])], [], {json.dumps(reason)});
""", expected=1)
    report, _ = summary(tmp_path)
    assert not report['complete'] and report['state'] == 'failed'
    assert report['outcomes']['total'] == (1 if state == 'passed' else 0)


def test_huge_multibyte_escaped_failures_and_captures_have_mechanical_total_bound(tmp_path):
    run_node(tmp_path, """
const reporter = new api.BoundedVitestReporter({directory});
reporter.onTestRunStart([{}]);
const huge = String.fromCharCode(233,92,34,10).repeat(100000);
for(let n=0;n<2000;n++) reporter.onUserConsoleLog({type:'stdout',content:huge});
const failed = Array.from({length:3000},(_,n)=>testCase('failed','failure '+n,[{message:huge,stack:huge}]));
reporter.onTestRunEnd([module(failed,'failed')], [], 'failed');
""", expected=1)
    report, detail = summary(tmp_path)
    assert report['outcomes']['failed'] == report['outcomes']['total'] == 3000
    assert report['counts_complete'] and not report['complete']
    assert report['details']['capture_events'] == 2000
    assert report['details']['omitted_records'] > 0 and report['details']['omitted_captures'] > 0
    assert report['details']['truncated_fields'] > 0
    assert all(len(record.get('content','').encode()) <= 2048 for record in detail['records'])
    assert all(len(record.get('message','').encode()) <= 16*1024 for record in detail['records'])


def test_case_limit_explicitly_marks_partial_counts(tmp_path):
    run_node(tmp_path, """
const reporter = new api.BoundedVitestReporter({directory});
reporter.onTestRunStart([{}]);
reporter.onTestRunEnd([module(Array.from({length:api.MAX_CASES+1},()=>testCase('passed')))], [], 'passed');
""", expected=1)
    report, _ = summary(tmp_path)
    assert report['case_limit_reached'] and not report['counts_complete'] and not report['complete']
    assert report['outcomes']['total'] == 10000


@pytest.mark.parametrize('fault', ['module-error','unhandled','missing-start','missing-module','empty'])
def test_collection_or_incomplete_protocol_cannot_report_passing(tmp_path, fault):
    expressions = {
        'module-error': "reporter.onTestRunStart([{}]); reporter.onTestRunEnd([module([], 'failed', [{message:'load failed'}])], [], 'failed');",
        'unhandled': "reporter.onTestRunStart([{}]); reporter.onTestRunEnd([module([testCase('passed')])], [{message:'unhandled'}], 'failed');",
        'missing-start': "reporter.onTestRunEnd([module([testCase('passed')])], [], 'passed');",
        'missing-module': "reporter.onTestRunStart([{}]); reporter.onTestRunEnd([], [], 'passed');",
        'empty': "reporter.onTestRunStart([]); reporter.onTestRunEnd([], [], 'passed');",
    }
    run_node(tmp_path, "const reporter = new api.BoundedVitestReporter({directory});" + expressions[fault], expected=1)
    report, _ = summary(tmp_path)
    assert report['state'] == 'failed'


def test_enospc_never_publishes_empty_or_passing_summary(tmp_path):
    run_node(tmp_path, """
const summaryDescriptors = new Set();
const io = {...fs,
 openSync(...args){const fd=fs.openSync(...args);if(String(args[0]).includes('.ui-summary.json.'))summaryDescriptors.add(fd);return fd;},
 writeSync(fd,...args){if(summaryDescriptors.has(fd)){const error=new Error('synthetic full');error.code='ENOSPC';throw error;}return fs.writeSync(fd,...args);}
};
const reporter = new api.BoundedVitestReporter({directory,io});reporter.onTestRunStart([{}]);
try{reporter.onTestRunEnd([module([testCase('passed')])], [], 'passed');throw Error('expected failure');}
catch(error){if(error.message!=='bounded_vitest_report_unavailable')throw error;}
""", expected=1)
    assert not (tmp_path / 'ui-summary.json').exists()
    assert (tmp_path / 'ui-detail.json').is_file()
    assert all(item.stat().st_size <= 64*1024 for item in tmp_path.glob('.ui-summary.json.*.tmp'))


def test_partial_writes_are_completed_and_existing_summary_is_never_replaced(tmp_path):
    run_node(tmp_path, """
const io={...fs,writeSync(fd,buffer,offset,length){return fs.writeSync(fd,buffer,offset,Math.min(length,7));}};
const reporter=new api.BoundedVitestReporter({directory,io});reporter.onTestRunStart([{}]);
reporter.onTestRunEnd([module([testCase('passed')])], [], 'passed');
const before=fs.readFileSync(path.join(directory,'ui-summary.json'),'utf8');
try{api.atomicJSON(directory,'ui-summary.json','{}',api.SUMMARY_BYTES);throw Error('expected refusal');}
catch(error){if(error.message!=='bounded_vitest_report_exists')throw error;}
if(fs.readFileSync(path.join(directory,'ui-summary.json'),'utf8')!==before)throw Error('overwritten');
""")
    assert summary(tmp_path)[0]['state'] == 'passed'


def test_production_default_refuses_missing_or_arbitrary_directory_env(tmp_path):
    run_node(tmp_path, """
for(const directory of [undefined,'/tmp','/test-output/../tmp']){
 if(directory===undefined)delete process.env.EH_CI_VITEST_REPORT_DIR;
 else process.env.EH_CI_VITEST_REPORT_DIR=directory;
 try{new api.default();throw Error('expected refusal');}
 catch(error){if(error.message!=='bounded_vitest_report_unavailable')throw error;}
}
""")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('failing', [False, True])
def test_real_cached_vitest_505_loads_external_reporter_without_package_import(tmp_path, failing):
    image_cli = Path('/opt/eh-ci/node_modules/vitest/vitest.mjs')
    cli = image_cli if image_cli.is_file() else ROOT / 'hub-ui/node_modules/vitest/vitest.mjs'
    if not cli.is_file(): pytest.skip('Cached Vitest absent; pure Node contracts still run')
    report_dir = tmp_path / 'reports'; report_dir.mkdir()
    adapter = tmp_path / 'reporter.mjs'
    adapter.write_text('import {BoundedVitestReporter} from ' + json.dumps(REPORTER.as_uri()) + ';\n'
        'export default class extends BoundedVitestReporter {constructor(){super({directory:' +
        json.dumps(str(report_dir)) + '});}}\n', encoding='utf-8')
    config = tmp_path / 'config.mjs'
    config.write_text('export default {test:{globals:true,include:["fixture.test.js"],environment:"node",pool:"threads",maxWorkers:1,fileParallelism:false}};',encoding='utf-8')
    (tmp_path / 'fixture.test.js').write_text('test("case",()=>{expect(2+2).toBe(' + ('5' if failing else '4') + ')});test.skip("skip",()=>{});test.todo("todo");',encoding='utf-8')
    result = subprocess.run([NODE, str(cli), 'run', '--root', str(tmp_path), '--config', str(config),
        '--reporter', str(adapter)], cwd=ROOT/'hub-ui', text=True, capture_output=True, timeout=45)
    assert result.returncode == (1 if failing else 0), result.stdout + result.stderr
    report, _ = summary(report_dir)
    assert report['state'] == ('failed' if failing else 'passed') and report['complete']
    assert report['outcomes']['failed' if failing else 'passed'] == 1
    assert report['outcomes']['skipped'] == 2


def test_summary_fsync_enospc_cannot_publish_success(tmp_path):
    run_node(tmp_path, """
const summaryDescriptors=new Set();
const io={...fs,
 openSync(...args){const fd=fs.openSync(...args);if(String(args[0]).includes('.ui-summary.json.'))summaryDescriptors.add(fd);return fd;},
 fsyncSync(fd){if(summaryDescriptors.has(fd)){const error=new Error('synthetic ENOSPC');error.code='ENOSPC';throw error;}return fs.fsyncSync(fd);}
};
const reporter=new api.BoundedVitestReporter({directory,io});reporter.onTestRunStart([{}]);
try{reporter.onTestRunEnd([module([testCase('passed')])],[],'passed');throw Error('expected fsync failure');}
catch(error){if(error.message!=='bounded_vitest_report_unavailable')throw error;}
""",expected=1)
    assert not (tmp_path/'ui-summary.json').exists()


@pytest.mark.skipif(os.environ.get('EH_TEST_PRODUCTION_REPORT_MOUNT') != '1',
                    reason='Requires an explicitly isolated Linux /test-output mount')
def test_real_cached_vitest_production_default_and_linux_directory_fsync(tmp_path):
    cli = Path('/opt/eh-ci/node_modules/vitest/vitest.mjs')
    assert cli.is_file() and Path('/test-output').is_dir()
    config = tmp_path / 'config.mjs'
    config.write_text('export default {test:{globals:true,include:["fixture.test.js"],environment:"node",pool:"threads",maxWorkers:1,fileParallelism:false}};')
    (tmp_path / 'fixture.test.js').write_text('test("case",()=>{expect(2+2).toBe(4)});test.skip("skip",()=>{});test.todo("todo");')
    env = dict(os.environ, EH_CI_VITEST_REPORT_DIR='/test-output')
    result = subprocess.run([NODE, str(cli), 'run', '--root', str(tmp_path), '--config', str(config),
        '--reporter', str(REPORTER)], cwd=ROOT/'hub-ui', text=True, capture_output=True, timeout=45, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    report, _ = summary(Path('/test-output'))
    assert report['state'] == 'passed' and report['complete']
    assert report['outcomes']['passed'] == 1 and report['outcomes']['skipped'] == 2
