"""Chart queries for the concert-only database."""

from __future__ import annotations

from concert_models import (
    ConcertEvent,
    ConcertIteration,
    ConcertTicket,
    CreateConcertModel,
)
from graph_builder import GraphBuilder
from models import hours_before_event


class ConcertGraphBuilder:
    def __init__(self):
        self.plotter = GraphBuilder()

    def single_concert_graph(
        self,
        venue: str,
        concert_id: int,
        section: str,
        display_mode: str,
    ) -> tuple[list[float], list[float]]:
        model = CreateConcertModel()
        with model.getSession()() as session:
            event = (
                session.query(ConcertEvent)
                .filter(
                    ConcertEvent.venue == venue,
                    ConcertEvent.id == concert_id,
                )
                .first()
            )
            if event is None:
                return [], []

            tickets = (
                session.query(ConcertTicket)
                .join(ConcertTicket.iteration)
                .join(ConcertIteration.event)
                .filter(
                    ConcertTicket.section == section,
                    ConcertEvent.id == concert_id,
                )
                .order_by(ConcertIteration.captured_at.asc())
                .all()
            )
            pairs = [
                (
                    round(
                        hours_before_event(
                            ticket.iteration.event.event_date,
                            ticket.iteration.captured_at,
                        ),
                        3,
                    ),
                    ticket.price,
                )
                for ticket in tickets
            ]

        pairs = [pair for pair in pairs if 0 < pair[0] <= 168]
        if not pairs:
            return [], []

        x, y = map(list, zip(*pairs))
        if display_mode != "money":
            y = self.plotter.standardize(y)
        return y, x

    def create_plot(self, x, y, display_mode):
        return self.plotter.create_plot(x, y, display_mode)
