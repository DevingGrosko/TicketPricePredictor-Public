from datetime import datetime
import json
from pathlib import Path

import EditSQLite
from WebBrowsing import SeleniumWeb
from DataBaseSQL import DataBaseSQL
from models import CreateModel, Event
from Prediction import Prediction

EXAMPLE_EVENT_URLS = {
    "Example Event": "https://example.com/tickets/example-event"
}


def addEvent(event_name, URL):
    scraper = SeleniumWeb()
    try:
        scraper.open(URL)
        eventTime = scraper.getEventDate(scraper.driver)
        scraper.run()
    finally:
        scraper.driver.quit()

    SQLModel = CreateModel().getSession()
    DB = DataBaseSQL()
    DB.add_event(event_name, SQLModel, eventTime, URL)


def addNewIteration(event_name, URL, scraper):
    scraper.open(URL)
    eventTime = scraper.getEventDate(scraper.driver)   # same driver as the network capture
    scraper.run()                 # writes vivid_listings.json
    SQLModel = CreateModel().getSession()

    DB = DataBaseSQL()

    DB.add_event(event_name,SQLModel,eventTime,URL)


def getPrediction(section, event_name):
    predict = Prediction()
    x_data, y_data = predict.get_Section(section,event_name)
    print(x_data)
    print(y_data)
    lowestDegree = predict.degree_finder(x_data,y_data)
    model = predict.train_model(x_data,y_data,lowestDegree)


def Automate():
    SQLSession = CreateModel().getSession()

    with SQLSession() as s:

        # Path to JSON file
        file_path = Path("quarter.json")

        # Load current value (or create if missing)
        if file_path.exists():
            with open(file_path, "r") as f:
                data = json.load(f)
        else:
            data = {"quarter": 0}

        quarter = data["quarter"]
        browser = SeleniumWeb()
        try:
            AllEvents = s.query(Event).all()
            now = datetime.now()
            for event in AllEvents:
                x = round((event.event_date - now).total_seconds() / 3600, 1)
                if x < 0:
                    pass
                elif x < 48:
                    print("48")
                    addNewIteration(event.title, event.URL,browser)
                elif x < 96 and (quarter == 0 or quarter == 2):
                    print("96")
                    addNewIteration(event.title, event.URL,browser)
                else:
                    if quarter == 0:
                        print("128")
                        addNewIteration(event.title, event.URL,browser)
        finally:
            browser.driver.quit()
        # Increment and wrap around
        data["quarter"] = (data["quarter"] + 1) % 4

        # Save back
        with open(file_path, "w") as f:
            json.dump(data, f)

if __name__ == "__main__":
    Automate()

# getPrediction("Upper Gallery Outfield 403","Phillies at Nats Aug16",2,10)
