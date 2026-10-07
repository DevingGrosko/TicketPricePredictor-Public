"""Publish original UI with a compact, capture-derived freshness indicator."""
from pathlib import Path
import argparse
import json
from unittest.mock import patch
from tools.free_live_mlb_scope import retain_scoped_events, tracked_venues
from tools import free_refresh_publish as publisher

FRESHNESS_JS = r'''
  function showPriceFreshness(boot, c, params) {
    const game = c.games?.[params.get('game') || boot.game || ''];
    const report = c.reports?.find(r => r.id === boot.report);
    const stamp = game ? game.captured_through : report ? report.captured_through : c.captured_through;
    const node = document.createElement('p');
    node.className = 'form-note price-freshness';
    node.setAttribute('role', 'status');
    node.style.cssText = 'max-width:1180px;margin:12px auto;padding:0 20px;font-size:0.85rem;';
    const date = stamp ? new Date(stamp) : null;
    const historical = game && new Date(game.event_at).getTime() < Date.now();
    let label = game ? (historical ? 'Historical game · last price snapshot: ' : 'Prices last captured: ')
                     : report ? report.team + ' latest price snapshot: '
                              : 'Latest ' + boot.sport.toUpperCase() + ' price snapshot: ';
    label += date && Number.isFinite(date.getTime()) ? date.toLocaleString() : 'unavailable';
    if (!game) label += ' · freshness varies by game';
    const lead = game ? (new Date(game.event_at).getTime() - Date.now()) / 3600000 : null;
    let interval = null;
    if (lead > 0 && lead <= 720) {
      if (boot.sport === 'nfl') interval = lead <= 168 ? 0.5 : lead <= 336 ? 3 : 6;
      if (boot.sport === 'nhl') interval = lead <= 72 ? 0.5 : lead <= 168 ? 6 : lead <= 336 ? 12 : 24;
    }
    let deadline = date && interval ? date.getTime() + (interval * 2 + 0.25) * 3600000 : null;
    const boundaries = boot.sport === 'nfl' ? [[336, 6], [168, 3]]
                     : boot.sport === 'nhl' ? [[336, 24], [168, 12], [72, 6]] : [];
    for (const [hoursBefore, previousInterval] of boundaries) {
      const boundary = game ? new Date(game.event_at).getTime() - hoursBefore * 3600000 : null;
      if (deadline && date.getTime() < boundary && boundary <= Date.now() &&
          boundary - date.getTime() <= (previousInterval * 2 + 0.25) * 3600000) {
        deadline = Math.max(deadline, boundary + (interval * 2 + 0.25) * 3600000);
      }
    }
    if (interval && (!date || !Number.isFinite(date.getTime()) || Date.now() > deadline || date.getTime() > Date.now() + 300000)) {
      label += ' · updates delayed';
      node.dataset.updatesDelayed = 'true';
    }
    if (game && game.section_count > 0 && game.section_count < 10) label += ' · limited section coverage';
    node.textContent = label;
    if (stamp) node.dataset.capturedAt = stamp;
    const main = document.querySelector('main');
    if (main) main.prepend(node);
  }
'''


def add_freshness(root):
    path = Path(root) / 'native/bridge.js'
    text = path.read_text()
    anchor = '  async function bootPage(boot, params) {'
    call = "    const c=boot.sport ? await catalog(boot.sport) : null;"
    if text.count(anchor) != 1 or text.count(call) != 1:
        raise ValueError('Unexpected original bridge; refusing an untested rewrite')
    text = text.replace(anchor, FRESHNESS_JS + '\n' + anchor, 1)
    text = text.replace(call, call + '\n    if (c) showPriceFreshness(boot,c,params);', 1)
    path.write_text(text)
    return publisher.validate_mounted(root)


class ScopedSnapshotCache(publisher.SnapshotCache):
    def read_sport(self, sport, *args, **kwargs):
        result = super().read_sport(sport, *args, **kwargs)
        scoped = retain_scoped_events(sport, result)
        if sport == 'mlb':
            self.metrics[sport]['out_of_scope_events_hidden'] = len(result[0]) - len(scoped[0])
        return scoped


def build(output, cache, scheduled=False):
    with patch.object(publisher, 'SnapshotCache', ScopedSnapshotCache):
        report = publisher.build(output, cache, scheduled=scheduled)
    report['mlb_tracked_venues'] = list(tracked_venues())
    report.update(add_freshness(output), capture_freshness_indicator=True)
    print('FREE_FRESHNESS_BUILD ' + json.dumps(report), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='free-pages-dist')
    parser.add_argument('--cache', default='.free-refresh-cache')
    parser.add_argument('--scheduled', action='store_true')
    args = parser.parse_args()
    build(args.output, args.cache, args.scheduled)
