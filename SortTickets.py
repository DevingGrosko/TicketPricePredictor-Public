import json


class SortTickets:
    def __init__(self):
        with open("vivid_listings.json", "r", encoding="utf-8") as f:
            self.vivid_listings = json.load(f)
        self.AllTicketsComputed = None
        self.SeatsPerSection = {}

    def getAllTickets(self) -> list:
        AllTickets = []
        if self.AllTicketsComputed is None:
            for ticket in self.vivid_listings["tickets"]:
                if "OBSTRUCTED_VIEW" not in (ticket.get("tags") or []):
                    last_word = ticket["l"].split()[-1]
                    if last_word.isdigit():
                        try:
                            AllTickets.append(self.create_ticket(ticket["aip"],ticket["l"]))
                        except Exception:
                            AllTickets.append(self.create_ticket(ticket["p"], ticket["l"]))
            self.AllTicketsComputed = AllTickets
            return AllTickets
        else: return self.AllTicketsComputed

    def AllSections(self):
        sectionsList = []
        for ticket in self.getAllTickets():
            if ticket["section"] not in sectionsList:
                sectionsList.append(ticket["section"])
        return sectionsList

    def NumSeatsPerSection(self) -> dict:
        if not self.SeatsPerSection:
            for section in self.AllSections():
                self.SeatsPerSection[section] = len(self.getSection(section))
            return self.SeatsPerSection
        else:
            return self.SeatsPerSection


    def getSection(self, inputSection:str) -> list:
        section = []
        for ticket in self.getAllTickets():
            if ticket["section"].strip().lower() == inputSection.strip().lower():
                section.append(ticket)
        return section

    def getCheapestTickets(self):
        CheapestTickets = []
        all_Sections = self.AllSections()
        for section in all_Sections:
                tickets = self.getSection(section)
                cheapest_ticket = tickets[0]
                # print(f"First ticket is: {cheapest_ticket}")
                for ticket in tickets:
                    # print(ticket)
                    if int(ticket["price"].split(".")[0]) < int(cheapest_ticket["price"].split(".")[0]):
                        cheapest_ticket = ticket
                CheapestTickets.append(cheapest_ticket)
        return CheapestTickets

    def getLevel(self,inputLevel:int) -> list:
        level = []
        inputLevel = str(inputLevel)[0]
        for ticket in self.getAllTickets():
            if int(ticket["section Number"][0]) == int(inputLevel):
                level.append(ticket)
        return level

    def create_ticket(self,ticketPrice,ticketSection):
        SingleTicket = {
            "price":ticketPrice,
            "section":ticketSection,
            "num in section" : ""
        }
        return SingleTicket
