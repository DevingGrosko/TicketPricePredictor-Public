"""Keep bounded diagnostic evidence without retaining every freshness failure."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tools.free_refresh_storage import ARTIFACT, MB, prune, stale_publication_artifacts

NOW = datetime(2026, 10, 8, 4, tzinfo=timezone.utc)


def artifact(identity, age_hours=1, size=45*MB, name=ARTIFACT, run=None):
    return {'id':identity, 'name':name, 'size_in_bytes':size,
            'created_at':(NOW-timedelta(hours=age_hours)).isoformat(),
            'workflow_run':{'id':run or identity}, 'expired':False}


def test_retains_only_latest_bounded_one_day_unverified_artifact():
    rows = [artifact(1,3), artifact(2,2), artifact(3,1), artifact(4,25),
            artifact(5,0.5,129*MB), artifact(6,0.1,name='unrelated-collector')]
    assert {row['id'] for row in stale_publication_artifacts(rows, NOW)} == {1,2,4,5}
    assert stale_publication_artifacts([artifact(7,24)], NOW)[0]['id'] == 7
    assert stale_publication_artifacts([artifact(8,0.1,size=128*MB)], NOW) == []


def test_prune_never_touches_current_active_or_unrelated_workflow_artifacts():
    rows = [artifact(1,3), artifact(2,2), artifact(3,1,run=99),
            artifact(4,1,run=98), artifact(5,1,run=97), artifact(6,name='another-site')]
    deleted = []
    def api(path, method='GET'):
        if method == 'DELETE':
            deleted.append(path)
            return None
        run = int(path.rsplit('/',1)[-1])
        return {'path':'.github/workflows/other.yml' if run == 97 else '.github/workflows/free-ticket-site.yml',
                'status':'in_progress' if run == 98 else 'completed'}
    def list_rows(path, key):
        return rows if key == 'artifacts' else []
    with patch.dict('os.environ', {'GITHUB_RUN_ID':'99'}), \
         patch('tools.free_refresh_storage.api', side_effect=api), \
         patch('tools.free_refresh_storage.list_rows', side_effect=list_rows), \
         patch('tools.free_refresh_storage.datetime') as clock:
        clock.now.return_value = NOW
        clock.fromisoformat.side_effect = datetime.fromisoformat
        prune()
    assert deleted == ['/actions/artifacts/1']
