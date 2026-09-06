from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import os
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker

from Flask_App.flask_app import app
from Flask_App.materialized_analytics import (
    TIMELINE_BUCKETS, SECTION_SUMMARY_STATE, SUMMARY_SCHEMA_VERSION,
    read_summary_rows, refresh_event_summary,
)
from Flask_App.nfl_blueprint import NFLEvent, NFLIteration, NFLTicket, CreateNFLModel
from Flask_App.nhl_blueprint import NHLEvent, NHLIteration, NHLTicket, CreateNHLModel
from Flask_App.nfl_stadium_blueprint import (
    _select_report, _generic_venue_index, _cached_venue_context,
    build_nfl_stadium_context, build_nhl_arena_context, build_mlb_stadium_context,
    build_nfl_section_context, build_nhl_section_context, build_mlb_section_context,
    nfl_display_venue, nfl_event_home_team,
)
from Flask_App.performance_cache import page_cache
from Flask_App.report_policy import (
    add_ranking_evidence, is_preseason, latest_season_events, report_venue,
)
from Flask_App.section_canonicalization import canonical_section_key
from models import Base, Event, Iteration, Ticket, event_datetime_utc, captured_datetime_for_storage

NOW = datetime(2026, 12, 1, tzinfo=timezone.utc)


def event(number, *, team='New York Giants', venue='MetLife Stadium', year=2026, kind=2):
    return SimpleNamespace(id=number, title='Dallas Cowboys at '+team,
        home_team=team, venue=venue, canonical_venue=venue, provider_venue=venue,
        country='US', event_date=datetime(year, 10, number, 18, tzinfo=timezone.utc),
        game_type=kind, schedule_id=None, sections=['Section 100'])


class RankingPolicyTests(unittest.TestCase):
    def calculate(self, *, games=3, observations=3, names=('Section 100',), sport='mlb'):
        events = [event(i) for i in range(1, games+1)]
        rows = [{'name':name, 'section_key':name} for name in names]
        points = [{'slot':s, 'price':100-10*i, 'observation_count':observations}
                  for i,s in enumerate(range(len(TIMELINE_BUCKETS[sport])-5, len(TIMELINE_BUCKETS[sport])))]
        prepared = {(name,e.id):deepcopy(points) for name in names for e in events}
        return events, rows, prepared

    def apply(self, events, rows, prepared, sport='mlb'):
        add_ranking_evidence(rows, prepared, events, sport=sport, now=NOW,
            buckets=TIMELINE_BUCKETS[sport], event_utc=event_datetime_utc)
        return rows[0]

    def test_fixed_windows_equal_game_weight_all_sports(self):
        for sport in ('mlb','nfl','nhl'):
            with self.subTest(sport=sport):
                e,r,p=self.calculate(sport=sport)
                result=self.apply(e,r,p,sport)
                self.assertAlmostEqual(result['ranking_price'],72.5)
                self.assertAlmostEqual(result['ranking_drop_percent'],40)
                self.assertTrue(result['ranking_price_eligible'])
                self.assertTrue(result['ranking_drop_eligible'])

    def test_one_game_cheap_section_cannot_win(self):
        e,r,p=self.calculate(names=('Section 100','Cheap 200'))
        p.pop(('Cheap 200',2)); p.pop(('Cheap 200',3))
        for point in p[('Cheap 200',1)]: point['price']=1
        self.apply(e,r,p)
        self.assertTrue(r[0]['ranking_price_eligible'])
        self.assertFalse(r[1]['ranking_price_eligible'])
        self.assertEqual(r[1]['ranking_price_games'],1)

    def test_sixty_percent_of_all_completed_games_required(self):
        e,r,p=self.calculate(games=10)
        for i in range(6,11): p.pop(('Section 100',i))
        result=self.apply(e,r,p)
        self.assertEqual(result['ranking_required_games'],6)
        self.assertFalse(result['ranking_price_eligible'])

    def test_missing_windows_and_three_observation_drop_rule(self):
        e,r,p=self.calculate()
        p[('Section 100',1)].pop(0)
        result=self.apply(e,r,p)
        self.assertTrue(result['ranking_price_eligible'])
        self.assertFalse(result['ranking_drop_eligible'])
        e,r,p=self.calculate(observations=2)
        self.assertFalse(self.apply(e,r,p)['ranking_drop_eligible'])
        for v in p.values(): v.pop()
        self.assertFalse(self.apply(e,r,p)['ranking_price_eligible'])

    def test_preseason_old_season_and_upcoming_never_count(self):
        e,r,p=self.calculate(sport='nhl')
        for number,year,kind in ((4,2025,2),(5,2026,1),(6,2026,2)):
            extra=event(number,year=year,kind=kind)
            if number==6: extra.event_date=NOW+timedelta(days=2)
            e.append(extra);p[('Section 100',number)]=deepcopy(p[('Section 100',1)])
        result=self.apply(e,r,p,'nhl')
        self.assertEqual(result['ranking_price_games'],3)
        self.assertEqual(result['ranking_total_games'],3)

    def test_duplicate_bucket_summaries_are_not_double_counted(self):
        e,r,p=self.calculate(); p[('Section 100',1)].append(deepcopy(p[('Section 100',1)][0]))
        self.assertEqual(self.apply(e,r,p)['ranking_price_games'],2)

    def test_detail_only_bare_label_still_uses_venue_ambiguity(self):
        e,r,p=self.calculate(names=('101',))
        for x in e: x.sections=['101','Club 101','Field Box 101']
        result=self.apply(e,r,p)
        self.assertTrue(result['ranking_ambiguous_label'])
        self.assertFalse(result['ranking_price_eligible'])

    def test_safe_typos_but_no_numeric_only_aliasing(self):
        key=lambda s:canonical_section_key('mlb','Fenway Park',s)
        self.assertEqual(key('FIRLD BOX 17'),key('Field Box 17'))
        self.assertNotEqual(key('Field Box 17'),key('Dugout Box 17'))
        self.assertNotEqual(key('17'),key('Field Box 17'))
        self.assertEqual(report_venue('Uniqlo Field at Dodger Stadium'),'Dodger Stadium')
        self.assertNotEqual(report_venue('GIANT Center'),report_venue('Capital One Arena'))


class TeamAndSeasonPolicyTests(unittest.TestCase):
    def test_shared_venue_reports_are_separate_and_legacy_url_asks_team(self):
        events=[event(1),event(2,team='New York Jets'),
                event(3,team='Los Angeles Rams',venue='SoFi Stadium'),
                event(4,team='Los Angeles Chargers',venue='SoFi Stadium')]
        with app.test_request_context():
            cards=_generic_venue_index(events,NOW,venue_getter=nfl_display_venue,
                team_getter=nfl_event_home_team,endpoint='nfl_stadium.nfl_stadium')
        self.assertEqual(len(cards),4)
        self.assertEqual(len({c['url'] for c in cards}),4)
        self.assertEqual([e.id for e in _select_report(events,'nfl','MetLife Stadium','New York Jets')['events']],[2])
        self.assertEqual(_select_report(events,'nfl','MetLife Stadium','')['events'],[])
        self.assertIn('Choose a home team',_select_report(events,'nfl','MetLife Stadium','')['selection_note'])

    def test_one_capitals_card_without_combining_two_real_arenas(self):
        events=[event(1,team='Washington Capitals',venue='Capital One Arena'),
                event(2,team='Washington Capitals',venue='GIANT Center')]
        with app.test_request_context():
            cards=_generic_venue_index(events,NOW,venue_getter=nfl_display_venue,
                team_getter=nfl_event_home_team,endpoint='nfl_stadium.nhl_arena')
        self.assertEqual(len(cards),1)
        self.assertEqual([e.id for e in _select_report(events,'nhl','GIANT Center','Washington Capitals')['events']],[2])

    def test_preseason_only_new_season_does_not_reuse_previous_year(self):
        rows=[event(1,year=2025),event(2,kind=1)]
        chosen,year=latest_season_events(rows,'nhl')
        self.assertEqual(year,2026);self.assertEqual(chosen,[])

    def test_cache_key_includes_home_team(self):
        page_cache.clear()
        with patch('Flask_App.nfl_stadium_blueprint._sport_venue_revision',return_value=1), \
             patch('Flask_App.nfl_stadium_blueprint.build_nfl_stadium_context',side_effect=lambda v,t:{'team':t}):
            self.assertEqual(_cached_venue_context('nfl','MetLife Stadium','New York Giants')['team'],'New York Giants')
            self.assertEqual(_cached_venue_context('nfl','MetLife Stadium','New York Jets')['team'],'New York Jets')
        page_cache.clear()

    def test_preseason_rules(self):
        e=event(1);e.event_date=datetime(2026,8,20)
        self.assertTrue(is_preseason('nfl',e))
        e.event_date=datetime(2026,9,9);self.assertFalse(is_preseason('nfl',e))
        e.game_type=1;self.assertTrue(is_preseason('nhl',e))
        e.game_type=None;e.schedule_id='2026010001';self.assertTrue(is_preseason('nhl',e))
        e.title='Spring Training - Mets at Nationals';self.assertTrue(is_preseason('mlb',e))
        e.title='Mets at Nationals';e.event_date=datetime(2026,3,27);self.assertFalse(is_preseason('mlb',e))

    def test_collectors_reject_explicit_preseason_schedule_entries(self):
        from nfl_schedule_collector import parse_schedule_payload as nfl_parse
        from nhl_schedule_collector import parse_schedule_payload as nhl_parse
        from test_nfl_schedule_collector import NFLScheduleParsingTests
        from test_nhl_schedule_collector import NHLScheduleCollectorTests
        now=datetime(2026,9,20,12,tzinfo=timezone.utc)
        n=NFLScheduleParsingTests()._event('1',now+timedelta(days=1),'Dallas Cowboys','New York Giants')
        n['season']={'type':1}
        self.assertEqual(nfl_parse({'events':[n]},now),[])
        h=NHLScheduleCollectorTests()._game(1,now+timedelta(days=1),'WSH','BOS',game_type=1)
        self.assertEqual(nhl_parse({'gameWeek':[{'games':[h]}]},now),[])


class StoredReportQualityTests(unittest.TestCase):
    def test_rankings_details_and_raw_preservation_for_every_sport(self):
        for sport in ('mlb','nfl','nhl'):
            with self.subTest(sport=sport), tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'test.db'
                env={'mlb':'DATABASE_PATH','nfl':'NFL_DATABASE_PATH','nhl':'NHL_DATABASE_PATH'}[sport]
                with patch.dict(os.environ,{env:str(path),'TICKETSIGNAL_DATABASE_BACKEND':'sqlite'}):
                    if sport=='mlb':
                        engine=create_engine(f'sqlite:///{path}');Base.metadata.create_all(engine)
                        E,I,T=Event,Iteration,Ticket;Session=sessionmaker(bind=engine,expire_on_commit=False)
                        venue,team='Nationals Park','Washington Nationals'
                    else:
                        M,E,I,T=(CreateNFLModel,NFLEvent,NFLIteration,NFLTicket) if sport=='nfl' else (CreateNHLModel,NHLEvent,NHLIteration,NHLTicket)
                        model=M(path);engine=model.engine;Session=model.getSession()
                        venue,team=('MetLife Stadium','New York Giants') if sport=='nfl' else ('Capital One Arena','Washington Capitals')
                    with Session() as session:
                        for n in range(4):
                            date=datetime(2025,11,10+n,18)
                            labels=['Section 100']+(['Cheap 200'] if n==0 else [])
                            args=dict(title=('New York Mets' if sport=='mlb' else 'Dallas Cowboys' if sport=='nfl' else 'New York Rangers')+' at '+team,event_date=date)
                            url=f'https://www.vividseats.com/--sports-mlb-baseball/production/{n+1}'
                            if sport=='mlb':args.update(Place=venue,URL=url,event_sections=labels)
                            else:args.update(home_team=team,away_team='Dallas Cowboys' if sport=='nfl' else 'New York Rangers',venue=venue,canonical_venue=venue,source_url=url,source_id=str(n+1),sections=labels,country='US')
                            if sport=='nhl':args.update(game_type=2,currency='USD')
                            e=E(**args);session.add(e);session.flush()
                            for j,(lower,upper,*_) in enumerate(TIMELINE_BUCKETS[sport][-5:]):
                                for delta in (-.5,0,.5):
                                    captured=captured_datetime_for_storage(event_datetime_utc(date)-timedelta(hours=(lower+upper)/2+delta))
                                    it=I(event_id=e.id,captured_at=captured);session.add(it);session.flush()
                                    for label in labels:
                                        a=dict(section=label,price=1 if label=='Cheap 200' else 100-10*j,iteration_id=it.id)
                                        a['ticketsPerSection' if sport=='mlb' else 'listing_count']=3
                                        session.add(T(**a))
                            session.flush()
                            refresh_event_summary(session,sport_key=sport,event_id=e.id,event_date=date,venue=venue,iteration_model=I,ticket_model=T,mark_complete=True)
                        session.commit();raw_count=session.query(T).count()
                    builder={'mlb':build_mlb_stadium_context,'nfl':build_nfl_stadium_context,'nhl':build_nhl_arena_context}[sport]
                    detail_builder={'mlb':build_mlb_section_context,'nfl':build_nfl_section_context,'nhl':build_nhl_section_context}[sport]
                    with app.test_request_context():
                        report=builder(venue,team);detail=detail_builder(venue,'Section 100',team)
                    self.assertEqual([r['name'] for r in report['cheapest_sections']],['Section 100'])
                    self.assertEqual([r['name'] for r in report['biggest_drops']],['Section 100'])
                    self.assertEqual(detail['section_summary']['ranking_price'],report['cheapest_sections'][0]['ranking_price'])
                    self.assertEqual(detail['section_summary']['ranking_drop_percent'],report['biggest_drops'][0]['ranking_drop_percent'])
                    self.assertEqual(parse_qs(urlsplit(detail['report_url']).query)['team'],[team])
                    with Session() as session:
                        self.assertEqual(session.query(T).count(),raw_count)
                        session.execute(update(SECTION_SUMMARY_STATE).values(summary_version=SUMMARY_SCHEMA_VERSION-1));session.commit()
                        self.assertEqual(read_summary_rows(session,[1,2,3,4]),[])
                    engine.dispose()

    def test_quality_endpoint_requires_authentication(self):
        with patch.dict(os.environ,{'COLLECTOR_INGEST_TOKEN':'test-only'}):
            client=app.test_client()
            self.assertEqual(client.get('/api/analytics/quality?sport=mlb').status_code,401)
            self.assertEqual(client.get('/api/analytics/quality?sport=bad',headers={'Authorization':'Bearer test-only'}).status_code,400)


if __name__=='__main__':
    unittest.main()
