"""Verify public report identity and authenticated compact sampling evidence."""
from collections import Counter
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlencode, urlsplit, parse_qs
from urllib.request import Request, urlopen


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.cards = []
        self.card = None
        self.heading = False
        self.row = None
        self.ranking_rows = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'a' and 'data-stadium-card' in a:
            self.card = {'team': '', 'url': a.get('href', '')}
        if self.card is not None and tag in ('h2', 'h3'):
            self.heading = True
        if tag == 'li' and 'nfl-ranking-row' in a.get('class', '').split():
            self.row = []
        if self.row is not None and tag == 'a':
            self.row.append({'url': a.get('href', ''), 'class': a.get('class', '')})

    def handle_endtag(self, tag):
        if tag in ('h2', 'h3'):
            self.heading = False
        if tag == 'a' and self.card is not None:
            self.cards.append(self.card)
            self.card = None
        if tag == 'li' and self.row is not None:
            self.ranking_rows.append(self.row)
            self.row = None

    def handle_data(self, text):
        if self.card is not None and self.heading:
            self.card['team'] += text.strip()


def main():
    base = 'https://bunnyjeff.pythonanywhere.com'
    token = os.environ.get('COLLECTOR_INGEST_TOKEN', '')
    if not token:
        raise RuntimeError('Missing collector authentication for the read-only audit')
    out = Path('report-quality-results')
    out.mkdir(exist_ok=True)
    result = {}

    def get(path, authenticated=False):
        headers = {'User-Agent': 'TicketSignal-report-quality-audit/1.0'}
        if authenticated:
            headers['Authorization'] = 'Bearer ' + token
        for attempt in range(3):
            try:
                with urlopen(Request(base + path, headers=headers), timeout=45) as response:
                    return response.read().decode()
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(2)

    for sport, path in [('mlb', '/'), ('nfl', '/nfl'), ('nhl', '/nhl')]:
        html = get(path)
        page = Page()
        page.feed(html)
        names = [re.sub(r'[^a-z0-9]+', ' ', c['team'].casefold()).strip() for c in page.cards]
        duplicates = [name for name, count in Counter(names).items() if count > 1]
        assert not duplicates, (sport, duplicates)
        assert page.cards, (sport, 'No team cards')
        reports = []
        for card in page.cards:
            query = parse_qs(urlsplit(card['url']).query)
            assert query.get('team') == [card['team']], card
            html = get(card['url'])
            report_page = Page()
            report_page.feed(html)
            for row in report_page.ranking_rows:
                assert len(row) == 1 and 'nfl-ranking-link' in row[0]['class'], row
                target = parse_qs(urlsplit(row[0]['url']).query)
                assert target.get('team') == [card['team']], (card, target)
                assert target.get('section'), target
            quality = json.loads(get('/api/analytics/quality?' + urlencode({'sport': sport, 'team': card['team']}), True))
            assert quality.get('status') == 'ok' and not quality.get('error'), quality
            assert quality.get('summary_schema_version') == 2, quality
            assert quality.get('team') == card['team'], (card, quality.get('team'))
            by_name = {row['name']: row for row in quality['sections']}
            for name in quality['cheapest']:
                row = by_name[name]
                assert row['ranking_price_eligible'] and row['ranking_price_games'] >= row['ranking_required_games'] >= 3, row
            for name in quality['drops']:
                row = by_name[name]
                assert row['ranking_drop_eligible'] and row['ranking_drop_games'] >= row['ranking_required_games'] >= 3, row
            if quality['cheapest']:
                first = report_page.ranking_rows[0][0]['url']
                detail = get(first)
                assert 'Comparable ranking evidence' in detail and 'No time-series points yet' not in detail, first
            reports.append(quality)
            print(sport, card['team'], 'games=', quality['completed_games'], 'priced sections=', quality['section_count'], 'price eligible=', quality['price_eligible_sections'], 'drop eligible=', quality['drop_eligible_sections'], flush=True)
        result[sport] = {'cards': page.cards, 'duplicates': duplicates, 'reports': reports}
        (out / (sport + '.json')).write_text(json.dumps(result[sport], indent=2))
    (out / 'summary.json').write_text(json.dumps({sport: {'teams':len(data['cards']), 'duplicate_teams':data['duplicates'], 'price_eligible_sections':sum(r['price_eligible_sections'] for r in data['reports']), 'drop_eligible_sections':sum(r['drop_eligible_sections'] for r in data['reports'])} for sport,data in result.items()}, indent=2))
    print('All three league directories and report eligibility checks passed.')


if __name__ == '__main__':
    main()
