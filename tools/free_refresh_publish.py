"""Prepare the original interface for free GitHub Pages publication.

Builds on the Actions runner, never on Render. Uses existing TiDB staging read
validation and a recoverable raw cache. No deployment or scheduling in this
module. Production source assets are not edited: three published navigation
scripts receive only the project-path prefix required by GitHub Pages.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path
import re
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from tools import build_static_preview as source
from tools.free_refresh_cache import SnapshotCache

PREFIX = '/TicketPricePredictor-Public'
CSP = "default-src 'self'; script-src 'self'; connect-src 'self'; style-src 'self' https://fonts.googleapis.com 'unsafe-inline'; font-src https://fonts.gstatic.com; img-src 'self' data:; object-src 'none'; base-uri 'self'"


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise source.BuildError('Reviewed source marker changed during Pages adaptation')
    return text.replace(old,new,1)


def adapt_bridge(text):
    helper = '''  const MOUNT = '/TicketPricePredictor-Public';
  const sitePath = p => {
    if (typeof p !== 'string' || !p.startsWith('/') || p.startsWith('//')) throw new Error('Invalid site path');
    return p === MOUNT || p.startsWith(MOUNT+'/') ? p : MOUNT+p;
  };
  const logicalPath = p => p.startsWith(MOUNT+'/') ? p.slice(MOUNT.length) : p;
  const navigate = p => location.replace(sitePath(p));
'''
    text = replace_once(text,'  const loader = document.currentScript;','  const loader = document.currentScript;\n'+helper)
    text = replace_once(text,'const url = new URL(path, location.origin);','const url = new URL(sitePath(path), location.origin);')
    text = replace_once(text,'.test(url.pathname)))','.test(logicalPath(url.pathname))))')
    text = replace_once(text,"manifest.live_updates_enabled !== false","typeof manifest.live_updates_enabled !== 'boolean'")
    text = replace_once(text,'const api = url.pathname.match(','const api = logicalPath(url.pathname).match(')
    text = replace_once(text,"url.pathname.startsWith('/api/')","logicalPath(url.pathname).startsWith('/api/')")
    text = replace_once(text,"back.href = '/';","back.href = sitePath('/');")
    # Rewrite only existing route transitions, not arbitrary data or calculations.
    for old,new in [
        ("location.replace('/reports/'+report.id+'.html')","navigate('/reports/'+report.id+'.html')"),
        ('location.replace(row.url)','navigate(row.url)'),
        ("location.replace('/maps/'+boot.sport+'-'+params.get('game')+'.html'+location.search)","navigate('/maps/'+boot.sport+'-'+params.get('game')+'.html'+location.search)"),
        ("location.replace('/'+sport+'/')","navigate('/'+sport+'/')"),
        ('script.src=path;','script.src=sitePath(path);'),
        ("value.selected_section=node.dataset.selectedSection || '';","value.selected_section=node.dataset.selectedSection || '';\n        if(value.graph_url) value.graph_url=sitePath(value.graph_url);")]:
        text = replace_once(text,old,new)
    return text


def mount_pages(root, *, scheduled=False):
    root = Path(root)
    bridge = root/'native/bridge.js'
    bridge.write_text(adapt_bridge(bridge.read_text()))
    # These original form handlers contain hard-coded root navigation. Only
    # published copies change; CSS, markup and chart calculations stay intact.
    expected_counts = {'script.js':3,'nfl.js':1,'nhl.js':1}
    for name,count in expected_counts.items():
        path = root/'static/js'/name
        text = path.read_text()
        text,n = re.subn(r'window\.location\.assign\(`/(graph|predict|nfl/graph|nhl/graph)\?',
                        lambda m:'window.location.assign(`'+PREFIX+'/'+m[1]+'?',text)
        if n != count:
            raise source.BuildError('Original form route count changed: '+name)
        path.write_text(text)
    assets = json.loads((root/'original-assets.json').read_text())
    for path in assets:
        assets[path] = hashlib.sha256((root/'static'/path).read_bytes()).hexdigest()
    (root/'original-assets.json').write_bytes(source.encoded(assets))
    # Every visible original link/form stays within the project site. Boot/data
    # attributes remain logical root paths; the adapter resolves them safely.
    attributes = re.compile(r'(?<![\w-])(href|src|action|data-map-base|data-options-url)="(/(?!/)[^"]*)"')
    for path in root.rglob('*.html'):
        text = attributes.sub(lambda m:m[1]+'="'+PREFIX+m[2]+'"',path.read_text())
        text = text.replace('<head>','<head>\n<meta http-equiv="Content-Security-Policy" content="'+html.escape(CSP,quote=True)+'">',1)
        path.write_text(text)
    for name in ('manifest.json','original-manifest.json'):
        path = root/name
        manifest = json.loads(path.read_text())
        manifest['live_updates_enabled'] = bool(scheduled)
        manifest['publication_host'] = 'github-pages'
        manifest['base_path'] = PREFIX
        manifest['refresh_interval_minutes'] = 30 if scheduled else None
        path.write_bytes(source.encoded(manifest))
    (root/'.nojekyll').write_text('')
    return validate_mounted(root)


def validate_mounted(root):
    root = Path(root).resolve(); count = total = 0
    seen = set()
    allowed = {'.html','.json','.js','.css','.txt','.svg','.png','.jpg','.jpeg','.webp','.ico'}
    for path in root.rglob('*'):
        if path.is_symlink():
            raise source.BuildError('Symlinks are not allowed in public output')
        if not path.is_file():continue
        if path.name != '.nojekyll' and path.suffix not in allowed:
            raise source.BuildError('Unexpected public file type')
        count += 1;total += path.stat().st_size
        if total > 700 * 1024**2:
            raise source.BuildError('Publication exceeds the free Pages safety budget')
        if path.suffix != '.html':continue
        for key,value in re.findall(r'\b(href|src|action|data-static-json|data-original-script|data-static-boot|data-map-base)="([^"]+)"',path.read_text()):
            parts = urlsplit(html.unescape(value))
            if parts.netloc or parts.scheme or not parts.path.startswith('/'):continue
            raw = unquote(parts.path)
            if raw.startswith(PREFIX+'/'): raw = raw[len(PREFIX):]
            elif key in ('href','src','action','data-map-base'):
                raise source.BuildError('Unprefixed public navigation target')
            if raw.startswith('/api/') or 'TSVALUE_' in raw:continue
            if raw in seen:continue
            seen.add(raw)
            target = root/raw.lstrip('/')
            if not target.resolve().is_relative_to(root) or not (target.is_file() or (target/'index.html').is_file()):
                raise source.BuildError('Missing mounted target: '+raw)
    manifest = json.loads((root/'original-manifest.json').read_text())
    for reference in manifest['sports'].values():
        raw = (root/reference.lstrip('/')).read_bytes()
        if hashlib.sha256(raw).hexdigest() not in reference:
            raise source.BuildError('Published catalog hash mismatch')
    return {'public_files':count,'public_bytes':total,'checked_targets':len(seen),'base_path':PREFIX}


def build(output,cache_directory,*,scheduled=False):
    from tools.build_original_fast import build as original_build
    from tools.check_original_static import validate
    output = Path(output).resolve(); cache = Path(cache_directory).resolve()
    if cache == output or cache.is_relative_to(output) or output.is_relative_to(cache):
        raise source.BuildError('Working cache and public output must be separate')
    reader = SnapshotCache(cache)
    with patch.object(source,'read_sport',reader.read_sport):
        report = original_build(output)
    validate(output)
    mounted = mount_pages(output,scheduled=scheduled)
    report.update(mounted,source_reads=reader.metrics,deployed=False)
    print('FREE_PAGES_BUILD '+json.dumps(report),flush=True)
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',default='free-pages-dist')
    p.add_argument('--cache',default='.free-refresh-cache')
    p.add_argument('--scheduled',action='store_true')
    a = p.parse_args()
    try:
        build(a.output,a.cache,scheduled=a.scheduled)
    except Exception as exc:
        import traceback
        print('FREE_PAGES_FAILED '+json.dumps({'type':type(exc).__name__,
              'message':str(exc) if isinstance(exc,source.BuildError) else 'Provider details withheld',
              'locations':[(f.name,f.lineno) for f in traceback.extract_tb(exc.__traceback__)[-5:]]}),flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
