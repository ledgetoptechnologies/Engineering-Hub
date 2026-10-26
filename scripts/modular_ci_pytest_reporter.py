"""Image-owned pytest launcher with bounded diagnostic JSON, never execution proof.

Invoke by absolute path under Python -I -B. Pytest is imported before tested
workspace paths are installed; pytest.main receives the caller's full arguments
unchanged. Source configuration/conftest/tests still execute normally. These
source-visible reports are diagnostics, not worker receipt or provenance authority.
The collected count is the selected item count; deselected items are separate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

MAX_SUMMARY_BYTES = 65536
MAX_DETAIL_BYTES = 131072
MAX_CASE_BYTES = 4096
MAX_ACTIVE_CASES = 128
MAX_DETAIL_ENTRIES = 256
SCHEMA = 'engineering-hub.modular-ci-python-report.v1'
_RANK = {'passed': 0, 'skipped': 1, 'xfailed': 2, 'xpassed': 3, 'failed': 4, 'errors': 5}


def clipped(text, limit):
    if type(text) is not str:
        return '[unavailable]', True
    # Slice before encoding: a huge original string never produces a huge copy.
    prefix = text[:limit]
    raw = prefix.encode('utf-8', errors='replace')
    result = raw[:limit].decode('utf-8', errors='ignore')
    return result, len(text) > len(result) or len(raw) > limit


class _Full(Exception):
    pass


class _Writer:
    """UTF-8 prefix collector that stops at its byte cap."""

    def __init__(self, limit):
        self.limit, self.bytes, self.parts, self.truncated = limit, 0, [], False

    def write(self, text, **kwargs):
        value, cut = clipped(text, self.limit - self.bytes)
        if value:
            self.parts.append(value)
        self.bytes += len(value.encode('utf-8'))
        if cut:
            self.truncated = True
            raise _Full()

    def line(self, text='', **kwargs):
        self.write(text)
        self.write('\n')

    def text(self):
        return ''.join(self.parts)


def trace_text(value, limit):
    if type(value) is str:
        return clipped(value, limit)
    writer = _Writer(limit)
    try:
        # Read pre-existing pytest representation strings directly: toterminal()
        # may build large intermediate lists or invoke a custom formatter before
        # reaching our bounded sink. Unsupported representations are explicit.
        entries = value.reprtraceback.reprentries
        if type(entries) not in (list, tuple):
            raise ValueError()
        writer.truncated = len(entries) > 64
        for entry in entries[:64]:
            lines = entry.lines
            if type(lines) not in (list, tuple):
                raise ValueError()
            writer.truncated |= len(lines) > 512
            for line in lines[:512]:
                writer.line(line)
        crash = getattr(value, 'reprcrash', None)
        if crash is not None:
            writer.line(crash.path)
            writer.line(crash.message)
        chain = getattr(value, 'chain', ())
        if type(chain) in (list, tuple) and len(chain) > 1:
            writer.truncated = True
    except _Full:
        pass
    except Exception:
        return '[trace renderer unavailable]', True
    return writer.text(), writer.truncated


def atomic_bytes(path, data, maximum):
    """Publish a complete bounded file; never replace an existing report."""
    if len(data) > maximum or not path.is_absolute() or path.exists() or path.is_symlink():
        raise ValueError('report_publication_invalid')
    temporary = path.parent / ('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists() or path.is_symlink():
            raise ValueError('report_publication_exists')
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


class Reporter:
    def __init__(self, path, *, detail_bytes=MAX_DETAIL_BYTES, case_bytes=MAX_CASE_BYTES):
        self.path = Path(path)
        if (not self.path.is_absolute() or type(detail_bytes) is not int
                or not 0 <= detail_bytes <= MAX_DETAIL_BYTES or type(case_bytes) is not int
                or not 512 <= case_bytes <= MAX_CASE_BYTES):
            raise ValueError('invalid_report_configuration')
        self.detail_path = self.path.with_suffix('.details.jsonl')
        if self.detail_path == self.path:
            raise ValueError('invalid_report_configuration')
        self.detail_budget, self.case_budget = detail_bytes, case_bytes
        self.outcomes = dict(collected=0, deselected=0, executed=0, collection_errors=0,
                             **{key: 0 for key in _RANK})
        self.reports = {phase: dict(passed=0, failed=0, skipped=0)
                        for phase in ('setup', 'call', 'teardown')}
        self.active = {}
        self.rows, self.detail_size, self.omitted, self.truncated = [], 0, 0, 0
        self.error = None
        self.summary = None
        self.collection_finished = False

    def _detail(self, report, phase):
        if (self.detail_size >= self.detail_budget or len(self.rows) >= MAX_DETAIL_ENTRIES):
            self.omitted += 1
            return
        name, cut = clipped(report.nodeid, min(384, self.case_budget // 4))
        trace, trace_cut = trace_text(report.longrepr, self.case_budget // 2)
        capture = _Writer(self.case_budget // 4)
        try:
            for heading, body in getattr(report, 'sections', ()):
                capture.line(heading)
                capture.line(body)
        except _Full:
            pass
        except Exception:
            capture.truncated = True
        row = dict(nodeid=name, phase=phase, traceback=trace, capture=capture.text(),
                   truncated=bool(cut or trace_cut or capture.truncated))
        while True:
            encoded = json.dumps(row, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\n'
            if len(encoded) <= self.case_budget:
                break
            # JSON escaping can expand bounded input. Reduce before publication.
            key = max(('nodeid', 'traceback', 'capture'), key=lambda key: len(row[key]))
            row[key] = row[key][:len(row[key]) // 2]
            row['truncated'] = True
        if len(encoded) > self.detail_budget - self.detail_size:
            self.omitted += 1
            return
        self.rows.append(encoded)
        self.detail_size += len(encoded)
        self.truncated += int(row['truncated'])

    def pytest_collection_finish(self, session):
        self.outcomes['collected'] = len(session.items)
        self.collection_finished = True

    def pytest_deselected(self, items):
        self.outcomes['deselected'] += len(items)

    def pytest_collectreport(self, report):
        if report.failed:
            self.outcomes['collection_errors'] += 1
            self._detail(report, 'collection')

    def pytest_runtest_logreport(self, report):
        phase, result = report.when, report.outcome
        if phase not in self.reports or result not in self.reports[phase]:
            self.error = 'invalid_report_event'
            return
        self.reports[phase][result] += 1
        if report.nodeid not in self.active:
            if len(self.active) >= MAX_ACTIVE_CASES:
                self.error = 'active_case_limit'
                return
            self.active[report.nodeid] = None
        outcome = None
        if result == 'failed':
            outcome = 'failed' if phase == 'call' else 'errors'
            self._detail(report, phase)
        elif result == 'skipped':
            outcome = 'xfailed' if hasattr(report, 'wasxfail') else 'skipped'
        elif phase == 'call':
            outcome = 'xpassed' if hasattr(report, 'wasxfail') else 'passed'
        previous = self.active[report.nodeid]
        if outcome is not None and (previous is None or _RANK[outcome] > _RANK[previous]):
            self.active[report.nodeid] = outcome

    def pytest_runtest_logfinish(self, nodeid, location):
        outcome = self.active.pop(nodeid, None)
        if outcome is None:
            self.error = 'missing_case_outcome'
            return
        self.outcomes[outcome] += 1
        self.outcomes['executed'] += 1

    def pytest_sessionfinish(self, session, exitstatus):
        original_exit = int(exitstatus)
        if self.active:
            self.error = 'unfinished_cases'
        if not self.collection_finished:
            self.error = 'collection_not_completed'
        if (self.outcomes['executed'] != self.outcomes['collected']
                or self.outcomes['executed'] != sum(self.outcomes[key] for key in _RANK)):
            self.error = 'case_count_mismatch'
        if self.outcomes['collection_errors']:
            self.error = 'collection_failed'
        if original_exit in {2, 3, 4} and self.error is None:
            self.error = 'pytest_session_incomplete'
        complete = self.error is None
        details = b''.join(self.rows)
        self.summary = dict(schema=SCHEMA, state='passed' if original_exit == 0 and complete else 'failed',
            complete=complete, pytest_exit_code=original_exit, outcomes=dict(self.outcomes),
            reports=self.reports, report_error=self.error,
            details=dict(file=self.detail_path.name, bytes=len(details),
                         sha256=hashlib.sha256(details).hexdigest(), entries=len(self.rows),
                         omitted=self.omitted, truncated=self.truncated))
        try:
            atomic_bytes(self.detail_path, details, self.detail_budget)
        except Exception:
            self.summary.update(state='failed', complete=False, report_error='details_write_failed')
        try:
            raw = json.dumps(self.summary, sort_keys=True, separators=(',', ':')).encode('utf-8') + b'\n'
            atomic_bytes(self.path, raw, MAX_SUMMARY_BYTES)
        except Exception:
            self.summary.update(state='failed', complete=False, report_error='summary_write_failed')
        if not self.summary['complete'] and original_exit == 0:
            session.exitstatus = 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--detail-bytes', type=int, default=MAX_DETAIL_BYTES)
    parser.add_argument('--case-bytes', type=int, default=MAX_CASE_BYTES)
    parser.add_argument('pytest_args', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if not args.source_root.is_absolute() or not args.source_root.is_dir():
        parser.error('An explicit absolute source directory is required')
    try:
        reporter = Reporter(args.report, detail_bytes=args.detail_bytes, case_bytes=args.case_bytes)
    except ValueError:
        parser.error('Invalid bounded report configuration')
    # Under -I, neither tested source nor cwd can shadow this import.
    import pytest
    source_paths = [str(args.source_root / 'src'), str(args.source_root)]
    sys.path[:0] = source_paths
    os.environ['PYTHONPATH'] = os.pathsep.join(source_paths)
    os.chdir(args.source_root)
    pytest_args = args.pytest_args[1:] if args.pytest_args[:1] == ['--'] else args.pytest_args
    result = int(pytest.main(pytest_args, plugins=[reporter]))
    if reporter.summary is None:
        return result if result != 0 else 1
    if not reporter.summary['complete'] and result == 0:
        return 1
    return result


if __name__ == '__main__':
    raise SystemExit(main())
