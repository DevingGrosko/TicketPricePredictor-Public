"""Stop the complete capture process group before allowing queue checkpoints."""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


def _subreaper():
    # Ubuntu runners must adopt/reap orphaned grandchildren when xvfb-run or
    # another intermediate parent exits first. A group leader's exit is not
    # evidence that its writers have stopped.
    if sys.platform == 'linux':
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            raise OSError(ctypes.get_errno(), 'Cannot become a capture subreaper')


def _group_exists(group):
    try:
        os.killpg(group, 0)
        return True
    except ProcessLookupError:
        return False


def _signal_group(group, signum):
    try:
        os.killpg(group, signum)
    except ProcessLookupError:
        pass


def _reap_orphans():
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if not pid:
            return


def _adopted_children():
    if sys.platform != 'linux':
        return []
    return [int(value) for value in Path(f'/proc/self/task/{os.getpid()}/children').read_text().split()]


def _stop_group(process, grace, settle):
    group = process.pid
    _signal_group(group, signal.SIGTERM)
    deadline = time.monotonic() + grace
    killed = False
    signalled = set()
    while True:
        # Popen owns its direct child's exit status; reap adopted children only
        # after poll() has collected that status.
        if process.poll() is not None:
            _reap_orphans()
        # A browser helper can create its own session. The Linux subreaper
        # adopts it after its parent exits, so it cannot escape shutdown merely
        # by leaving the original process group.
        adopted = _adopted_children() if process.returncode is not None else []
        for pid in adopted:
            signum = signal.SIGKILL if killed else signal.SIGTERM
            if (pid, signum) not in signalled:
                try:
                    os.kill(pid, signum)
                except ProcessLookupError:
                    pass
                signalled.add((pid, signum))
        if not _group_exists(group) and not adopted:
            process.wait()
            _reap_orphans()
            return killed
        if time.monotonic() >= deadline:
            if killed:
                raise RuntimeError('Capture group has not stopped; checkpoints remain forbidden')
            _signal_group(group, signal.SIGKILL)
            killed = True
            deadline = time.monotonic() + settle
        time.sleep(0.02)


def _context():
    return {key: os.environ.get(key) for key in ('GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_JOB')}


def _write_marker(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.capture-stopped-', delete=False) as output:
        temporary = Path(output.name)
        output.write((json.dumps(report, sort_keys=True) + '\n').encode())
        output.flush()
        os.fsync(output.fileno())
    try:
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def verify_stopped(marker):
    path = Path(marker)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
        raise RuntimeError('Missing capture shutdown proof; checkpoints remain forbidden')
    value = json.loads(path.read_bytes())
    group = value.get('process_group')
    if (value.get('version') != 1 or value.get('processes_stopped') is not True
            or value.get('context') != _context() or type(group) is not int or group <= 0
            or _group_exists(group)):
        raise RuntimeError('Invalid capture shutdown proof; checkpoints remain forbidden')
    return value


def run(command, marker, *, seconds=1500, grace=5, settle=5):
    if (not command or not 0 < seconds <= 1500 or not 0 <= grace <= 15
            or not 0 < settle <= 15 or not hasattr(os, 'killpg')):
        raise ValueError('Invalid bounded POSIX capture command or deadline')
    path = Path(marker)
    if path.is_symlink():
        raise ValueError('Capture shutdown proof cannot be a symlink')
    path.unlink(missing_ok=True)
    _subreaper()
    interrupted = []
    def on_signal(signum, _frame):
        if not interrupted:
            interrupted.append(signum)
    handlers = {kind: signal.signal(kind, on_signal) for kind in (signal.SIGTERM, signal.SIGINT)}
    process = None
    began = time.monotonic()
    timed_out = False
    cleanup_attempted = False
    try:
        process = subprocess.Popen(command, start_new_session=True)
        while process.poll() is None and not interrupted:
            remaining = seconds - (time.monotonic() - began)
            if remaining <= 0:
                timed_out = True
                break
            time.sleep(min(0.05, remaining))
        original_code = process.poll()
        cleanup_attempted = True
        killed = _stop_group(process, grace, settle)
        if interrupted:
            code = 128 + interrupted[0]
        elif timed_out:
            code = 124
        else:
            code = original_code if original_code is not None else process.returncode
            code = 128 - code if code < 0 else code
        report = dict(version=1, context=_context(), process_group=process.pid,
                      processes_stopped=True, exit_code=code, timed_out=timed_out,
                      received_signal=interrupted[0] if interrupted else None,
                      killed=killed, seconds=round(time.monotonic() - began, 3),
                      finished_at=datetime.now(timezone.utc).isoformat())
        _write_marker(path, report)
        print('BOUNDED_CAPTURE ' + json.dumps(report), flush=True)
        return code
    finally:
        # Exceptions also stop descendants, and cannot create a success marker.
        try:
            if process is not None and not cleanup_attempted:
                _stop_group(process, grace, settle)
        finally:
            for kind, handler in handlers.items():
                signal.signal(kind, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='operation', required=True)
    execute = subparsers.add_parser('run')
    execute.add_argument('--seconds', type=float, default=1500)
    execute.add_argument('--grace-seconds', type=float, default=5)
    execute.add_argument('--settle-seconds', type=float, default=5)
    execute.add_argument('--marker', required=True)
    execute.add_argument('command', nargs=argparse.REMAINDER)
    verify = subparsers.add_parser('verify')
    verify.add_argument('--marker', required=True)
    args = parser.parse_args()
    if args.operation == 'verify':
        verify_stopped(args.marker)
        return 0
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    return run(command, args.marker, seconds=args.seconds,
               grace=args.grace_seconds, settle=args.settle_seconds)


if __name__ == '__main__':
    raise SystemExit(main())
