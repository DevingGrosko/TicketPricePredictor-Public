"""Publish original UI with a compact, capture-derived freshness indicator."""
from pathlib import Path
import argparse
import json
from tools import free_refresh_publish as publisher

FRESHNESS_JS = r'''
  function showPriceFreshness(boot, c, params) {
    const game = c.games?.[params.get('game') || boot.game || ''];
    const stamp = game?.captured_through || c.captured_through;
    const node = document.createElement('p');
    node.className = 'form-note price-freshness';
    node.setAttribute('role', 'status');
    node.style.cssText = 'max-width:1180px;margin:12px auto;padding:0 20px;font-size:0.85rem;';
    const date = stamp ? new Date(stamp) : null;
    const historical = game && new Date(game.event_at).getTime() < Date.now();
    let label = game ? (historical ? 'Historical game · last price snapshot: ' : 'Prices last captured: ')
                     : 'Latest ' + boot.sport.toUpperCase() + ' price snapshot: ';
    label += date && Number.isFinite(date.getTime()) ? date.toLocaleString() : 'unavailable';
    if (!game) label += ' · freshness varies by game';
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


def build(output, cache, scheduled=False):
    report = publisher.build(output, cache, scheduled=scheduled)
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
