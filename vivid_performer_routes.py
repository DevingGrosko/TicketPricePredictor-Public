"""Opt-in ordinary team-to-event navigation from observed public directory links."""
from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit


@lru_cache(maxsize=1)
def _routes():
    path = Path(__file__).with_name('data') / 'vivid_performer_routes.json'
    value = json.loads(path.read_text())
    if set(value) != {'nfl', 'nhl'}:
        raise ValueError('The public navigation directory has unexpected sports')
    for sport, record in value.items():
        if len(record['teams']) != 32:
            raise ValueError('The public navigation directory is incomplete')
        for team, url in record['teams'].items():
            parsed = urlsplit(url)
            category = 'sports-nfl-football' if sport == 'nfl' else 'sports-nhl-hockey'
            if (not isinstance(team, str) or not team.strip()
                    or parsed.scheme != 'https' or parsed.netloc != 'www.vividseats.com'
                    or parsed.query or parsed.fragment
                    or not re.fullmatch(r'/[a-z0-9-]+--' + category + r'/performer/[0-9]+', parsed.path)):
                raise ValueError('Invalid public team navigation link')
    return value


def performer_url(sport, home_team):
    try:
        return _routes()[sport]['teams'][home_team]
    except (KeyError, TypeError):
        raise ValueError('No observed public navigation link for this team') from None


def configure_schedule_navigation(browser, sport, game, event_url):
    mode = os.environ.get('TICKETSIGNAL_FIREFOX_NAVIGATION', 'direct').strip().casefold()
    webkit = getattr(browser, '_webkit_session', None)
    # WebKit supports only the ordinary public route validated in its canary.
    if webkit is not None:
        mode = 'performer'
    if mode == 'direct':
        return
    if mode != 'performer':
        raise ValueError('TICKETSIGNAL_FIREFOX_NAVIGATION must be direct or performer')
    if webkit is None and getattr(browser, '_firefox_session', None) is None:
        raise ValueError('Team navigation requires an explicitly selected Firefox or WebKit engine')
    parsed = urlsplit(event_url)
    match = re.fullmatch(r'/(?:[a-zA-Z0-9_-]+/)*[a-zA-Z0-9_-]+/production/([0-9]+)', parsed.path.rstrip('/'))
    if (parsed.scheme != 'https' or parsed.netloc != 'www.vividseats.com'
            or parsed.query or parsed.fragment or match is None):
        raise ValueError('Team navigation requires an exact public event URL')
    stamp = game.event_date
    if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError('Team navigation requires an aware official event date')
    production_id = match.group(1)
    routes = {production_id: performer_url(sport, game.home_team)}
    dates = {production_id: stamp.astimezone(timezone.utc)}
    if webkit is not None:
        if sport == 'nhl':
            context = dict(sport='nhl', schedule_id=getattr(game, 'schedule_id', None),
                event_date=stamp.astimezone(timezone.utc).isoformat(), away_team=getattr(game, 'away_team', None),
                home_team=game.home_team, venue=getattr(game, 'venue', None),
                venue_timezone=getattr(game, 'venue_timezone', None))
            webkit.configure_normal_navigation(routes, dates, official_games={production_id: context})
        else:
            webkit.configure_normal_navigation(routes, dates)
    else:
        from vivid_firefox import configure_normal_navigation
        configure_normal_navigation(browser, performer_urls=routes, expected_event_dates=dates)
