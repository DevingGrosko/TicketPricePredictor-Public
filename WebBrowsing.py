from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from selenium.webdriver.common.by import By
from selenium import webdriver


class SeleniumWeb:
    def __init__(self):
        self.options = webdriver.ChromeOptions()
        self.driver = webdriver.Chrome(options=self.options)

    def open(self, event_url):
        self.driver.get(event_url)

    def run(self):
        raise NotImplementedError(
            "The production listing collector is excluded from the public repo. "
            "Use an exported JSON listing file or implement a source-specific collector."
        )

    def parse_event_time(self,s: str) -> datetime:
        tz = ZoneInfo("America/New_York")

        s = s.strip()

        if s.startswith("Today at "):
            # "Today at 7:05pm 2025" -> use today's date in tz
            t = datetime.strptime(s[len("Today at "):], "%I:%M%p %Y")
            d = datetime.now(tz).date()
            return datetime(d.year, d.month, d.day, t.hour, t.minute, tzinfo=tz)

        if s.startswith("Tomorrow at "):
            t = datetime.strptime(s[len("Tomorrow at "):], "%I:%M%p %Y")
            d = (datetime.now(tz) + timedelta(days=1)).date()
            return datetime(d.year, d.month, d.day, t.hour, t.minute, tzinfo=tz)

        # fallback for absolute strings like "Thu, Aug 14 at 6:45pm 2025"
        dt = datetime.strptime(s, "%a, %b %d at %I:%M%p %Y")
        return dt.replace(tzinfo=tz)

    def getEventDate(self, selenium_driver) -> datetime:
        time.sleep(1)
        el = selenium_driver.find_element(By.XPATH,
                                          '//*[@id="listings-header"]/div[1]/div[1]/div/div[3]/h1/span/div[2]')
        temp_time = el.text.split("\n")[0] + " 2025"
        temp_time = self.parse_event_time(temp_time)
        return temp_time
