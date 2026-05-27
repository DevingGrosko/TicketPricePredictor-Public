from sqlalchemy import select

from models import Event,Iteration,Ticket
from SortTickets import SortTickets
import json

class DataBaseSQL:

    def __init__(self):
        with open("vivid_listings.json", "r", encoding="utf-8") as f:
            self.vivid_listings = json.load(f)

    def add_tickets(self,iteration, session,ticket_list):
       for ticket in ticket_list:
           SortTicketsClass = SortTickets()
           NumInSection = SortTicketsClass.NumSeatsPerSection()[ticket["section"]]
           ticket["num in section"] = NumInSection
           one_ticket = Ticket(section=ticket["section"], price=ticket["price"],
                               iteration=iteration,ticketsPerSection=ticket["num in section"])
           session.add(one_ticket)

    def create_event(self,title: str, session,date,all_sections,URL) -> Event:
        place = self.vivid_listings["global"][0]["mapTitle"]
        event = Event(title=title,event_date=date,event_sections=all_sections,URL=URL,Place=place)
        session.add(event)
        return event

    def create_iteration(self,event, session):
        iteration = Iteration(event=event)  # link directly via object
        session.add(iteration)
        return iteration

    def add_event(self,title: str,SQLModel,date,URL):
        Tickets = SortTickets()
        all_sections = Tickets.AllSections()
        ticket_list = Tickets.getCheapestTickets()

        with SQLModel() as session:
            event = session.query(Event).filter_by(title=title).first()
            if event is None:
                event = self.create_event(title, session,date,all_sections,URL)
            else:
                #If a section now has tickets where it didn’t before, add it
                for section in all_sections:
                    if section not in event.event_sections:
                        event.event_sections.append(section)


            iteration = self.create_iteration(event, session)

            self.add_tickets(iteration, session,ticket_list)

            # Save all
            session.commit()

            print("Event ID:", event.id)
            print("Iteration ID:", iteration.id)

