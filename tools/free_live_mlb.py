"""Free-runner MLB collection: drain every candidate and isolate each failure.

The original production collector is unchanged. Expired event metadata is
checked before waiting for listings that may no longer exist. Only timeouts get
one fresh-browser retry; valid captures are queued before independent delivery.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import collector as mlb


class OutsideCaptureWindow(RuntimeError):
    def __init__(self, event_date, reason):
        super().__init__(reason)
        self.event_date, self.reason = event_date, reason


def require_upcoming(event_date, now=None):
    now = now or datetime.now(timezone.utc)
    hours = (mlb.as_utc(event_date) - now).total_seconds() / 3600
    if hours <= 0:
        raise OutsideCaptureWindow(event_date, 'event-has-started')
    if hours > mlb.CAPTURE_WINDOW_HOURS:
        raise OutsideCaptureWindow(event_date, 'outside-exact-capture-window')


class UpcomingMLBBrowser(mlb.VividBrowser):
    def _event_datetime(self, url):
        event_date = super()._event_datetime(url)
        # RuntimeError intentionally escapes the base browser's metadata-not-
        # rendered ValueError retry. Never infer a kickoff time from a URL date.
        require_upcoming(event_date)
        return event_date


def run_remote_mlb(endpoint, token, headless, timeout, health_output, pending_dir):
    from tools.free_live_collect import read_json, write_json
    started = datetime.now(timezone.utc)
    slot = started.replace(minute=0 if started.minute < 30 else 30, second=0, microsecond=0)
    pending_dir = Path(pending_dir)
    root = pending_dir.parent
    state_path = root / 'mlb-progress.json'
    state = read_json(state_path)
    backlog = state.setdefault('pending', {})
    completed = state.setdefault('completed', {})
    replayed, _available, replay_errors = mlb.replay_pending_snapshots(endpoint, token, pending_dir)
    report = {'status': 'running', 'event_type': 'mlb', 'started_at': started.isoformat(),
              'capture_slot': slot.isoformat(), 'discovered': 0, 'due': 0,
              'captured': 0, 'succeeded': 0, 'uploaded': 0, 'queued': 0,
              'replayed': replayed, 'skipped': 0, 'already_committed': 0,
              'failed': 0, 'discovery_failed': 0, 'retries': 0,
              'uploads': [], 'skipped_events': [], 'errors': list(replay_errors)}

    def checkpoint():
        report['pending'] = len(list(pending_dir.glob('*.json')))
        report['unfinished_games'] = sorted(backlog)
        write_json(state_path, state)
        write_json(health_output, report)

    checkpoint()
    discovered = set()
    for venue, venue_url in mlb.VENUE_FEEDS.items():
        browser = None
        try:
            browser = mlb.VividBrowser(headless=headless, timeout=timeout)
            urls = browser.discover_event_urls(venue_url)
            discovered.update(urls)
            print('FREE_MLB_DISCOVERY ' + json.dumps({'venue': venue, 'links': len(urls)}), flush=True)
        except Exception as exc:
            report['discovery_failed'] += 1
            report['errors'].append(venue + ': ' + type(exc).__name__)
        finally:
            if browser is not None:
                try: browser.close()
                except Exception: pass

    today = started.astimezone(mlb.NEW_YORK).date()
    horizon = today + timedelta(days=3)
    due = {url for url in discovered if not mlb.registry_row_is_excluded({'url': url})
           and (hint := mlb.event_date_from_url(url)) is not None
           and today <= hint.date() <= horizon}
    report['discovered'] = len(discovered)
    for url in due:
        if completed.get(url) == slot.isoformat():
            report['already_committed'] += 1
        else:
            backlog.setdefault(url, slot.isoformat())
    # Keep prior unresolved candidates even when discovery is temporarily empty.
    for url in list(backlog):
        hint = mlb.event_date_from_url(url)
        if hint is not None and hint.date() < today:
            backlog.pop(url)
            report['skipped_events'].append({'url': url, 'reason': 'event-date-has-passed'})
    work = sorted(backlog, key=lambda url: (backlog[url], url))
    report['due'] = len(work) + report['already_committed']
    checkpoint()
    retry = []
    for pass_number in (1, 2):
        for url in (work if pass_number == 1 else retry):
            browser = None
            captured_payload = False
            try:
                browser = UpcomingMLBBrowser(headless=headless, timeout=timeout)
                raw, event_date = browser.capture(mlb.validated_vivid_url(url))
                observed = datetime.now(timezone.utc)
                require_upcoming(event_date, observed)
                snapshot = mlb.SnapshotParser.parse(raw)
                payload = mlb.snapshot_to_payload(url, event_date, observed, snapshot)
                queued_path = mlb.queue_snapshot(payload, pending_dir)
                captured_payload = True
                report['captured'] += 1
                # No endpoint-wide latch: one bad game cannot suppress another.
                response = mlb.post_snapshot_with_retry(endpoint, token, payload)
                if response.get('status') not in ('stored', 'duplicate'):
                    raise RuntimeError('Unacknowledged snapshot')
                observed_slot = observed.replace(minute=0 if observed.minute < 30 else 30,
                                                 second=0, microsecond=0)
                completed[url] = observed_slot.isoformat()
                backlog.pop(url, None)
                checkpoint()  # Keep a receipt even if the runner exits now.
                queued_path.unlink(missing_ok=True)
                report['succeeded'] += 1
                report['uploaded'] += 1
                item = {'url': url, 'result': response['status'], 'title': snapshot.title,
                        'sections': len(snapshot.sections), 'captured_at': observed.isoformat()}
                report['uploads'].append(item)
                print('FREE_MLB_GAME ' + json.dumps(item), flush=True)
            except OutsideCaptureWindow as exc:
                backlog.pop(url, None)
                report['skipped'] += 1
                item = {'url': url, 'reason': exc.reason, 'event_date': exc.event_date.isoformat()}
                report['skipped_events'].append(item)
                print('FREE_MLB_SKIPPED ' + json.dumps(item), flush=True)
            except Exception as exc:
                is_timeout = isinstance(exc, TimeoutError) or type(exc).__name__ == 'TimeoutException'
                if pass_number == 1 and is_timeout and not captured_payload:
                    retry.append(url)
                    report['retries'] += 1
                else:
                    report['failed'] += 1
                    report['queued'] += int(captured_payload)
                    report['errors'].append(url + ': ' + type(exc).__name__)
                print('FREE_MLB_GAME_ERROR ' + json.dumps({'url': url, 'attempt': pass_number,
                      'type': type(exc).__name__, 'will_retry': pass_number == 1 and is_timeout
                      and not captured_payload}), flush=True)
            finally:
                if browser is not None:
                    try: browser.close()
                    except Exception: pass
                checkpoint()
    report['status'] = 'healthy' if not (report['failed'] or report['discovery_failed']
                         or report['pending'] or backlog or report['errors']) else 'degraded'
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    checkpoint()
    print('FREE_MLB_RESULT ' + json.dumps(report), flush=True)
    return 0 if report['status'] == 'healthy' else 1
