from itertools import zip_longest

from models import CreateModel, Ticket, Iteration, Event
import io
import base64
import matplotlib
matplotlib.use("Agg")   # use a backend that doesn’t need a window
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker


class GraphBuilder:
    def __init__(self):
        pass

    def standardize(self,price_list):
        if not price_list:
            return []

        standard_price = price_list[0]
        if standard_price == 0:
            return price_list

        standardized_list = [100]
        for num in price_list[1:]:
            standardized_list.append(round((num / standard_price) * 100))
        return standardized_list


    def allEventsForStadium(self,stadium,section,time:int,x_average_or_percentage):
        SessionLocal = CreateModel().getSession()
        with SessionLocal() as s:
            events = s.query(Event).filter(Event.Place == stadium).all()
            bins = {}
            total = 0
            for i in reversed(range(time)):
                bins[i + 0.75] = []
                bins[i + 0.5] = []
                bins[i + 0.25] = []
                bins[i] = []

            for event in events:

                x_time,y_price = self.eachEventGraphList(section,event.id)
                if not x_time or not y_price:
                    continue

                filtered_pairs = [(x, y) for x, y in zip(x_time, y_price) if x <= time]

                # Step 2: Unzip into separate x and y lists
                if filtered_pairs:
                    total += 1
                    filtered_x, filtered_y = zip(*filtered_pairs)  # Gives tuples
                    filtered_y = list(filtered_y)  # Convert to list so we can standardize

                    # Step 3: Standardize if needed
                    if x_average_or_percentage != "money":
                        filtered_y = self.standardize(filtered_y)

                    # Step 4: Repack the standardized values
                    filtered_pairs = list(zip(filtered_x, filtered_y))
                # Unpack into separate lists
                i = 0
                j = 0
                if filtered_pairs:
                    current_key = list(bins.keys())
                    while j < len(current_key):
                        if i < len(filtered_pairs):
                            if float(current_key[j] - 0.125) <= float(filtered_pairs[i][0]) < float(
                                    current_key[j] + 0.125):
                                bins[current_key[j]].append(filtered_pairs[i][1])
                                i += 1
                                j += 1
                            elif float(filtered_pairs[i][0]) > float(current_key[j] + 0.125):
                                i += 1
                            else:
                                j += 1
                        else:
                            break
            min_samples = 2 if total >= 2 else 1
            bins = self.guaranteeFairAverage(bins,min_samples)
            each_key = list(bins.keys())
            average_key_list = []
            for key in each_key:
                average_key_list.append(self.average(bins.get(key)))

        return average_key_list,each_key,total

    def guaranteeFairAverage(self,myDict:dict,num):
        each_key = list(myDict.keys())
        for key in each_key:
            if len(myDict.get(key)) < num:
                myDict.pop(key, None)
        return myDict


    def average(self,key_list:list):
        total = 0
        for i in key_list:
            total += i
        return total / len(key_list)


    def singleGameGraph(self,stadium,event_id,section,x_average_or_percentage):
        SessionLocal = CreateModel().getSession()
        with SessionLocal() as s:
            event = s.query(Event).filter(Event.Place == stadium, Event.id == event_id).first()
            if event is None:
                return [], []

            x_time, y_price = self.eachEventGraphList(section, event.id)
            if not x_time or not y_price:
                return [], []

            usable_pairs = [
                (hours_until, price)
                for hours_until, price in zip(x_time, y_price)
                if 0 < hours_until <= 96
            ]
            if not usable_pairs:
                return [], []
            x_time, y_price = map(list, zip(*usable_pairs))

            if x_average_or_percentage == "money":
                pass
            else:
                y_price = self.standardize(y_price)

            return y_price, x_time



    def create_plot(self, x, y, x_type, analysis_mode="multi"):
        background = "#111827"
        text_color = "#aab4c8"
        line_color = "#a78bfa"

        fig, ax = plt.subplots(figsize=(9, 5.2), facecolor=background)
        ax.set_facecolor(background)
        ax.plot(
            x,
            y,
            marker="o",
            markersize=4,
            markerfacecolor=background,
            markeredgecolor=line_color,
            markeredgewidth=1.5,
            color=line_color,
            linewidth=2.4,
        )
        ax.fill_between(x, y, min(y), color=line_color, alpha=0.08)
        ax.set_xlabel("Hours until event", color=text_color, labelpad=12)
        if x_type == "percentage":
            ax.set_ylabel("Relative price (100 = starting point)", color=text_color, labelpad=12)
            ax.yaxis.set_major_formatter(mticker.StrMethodFormatter("{x:,.0f}%"))
        else:
            price_label = "Lowest listed price" if analysis_mode == "single" else "Average lowest listed price"
            ax.set_ylabel(price_label, color=text_color, labelpad=12)
            ax.yaxis.set_major_formatter(mticker.StrMethodFormatter("${x:,.0f}"))

        ax.invert_xaxis()  # count down toward the event
        ax.grid(True, color="#2a354b", linestyle="-", linewidth=0.7, alpha=0.72)
        ax.xaxis.set_major_locator(plt.MaxNLocator(10))
        ax.yaxis.set_major_locator(plt.MaxNLocator(8))
        ax.tick_params(colors=text_color, labelsize=9)
        for spine in ax.spines.values():
            spine.set_color("#2a354b")
        fig.tight_layout(pad=1.4)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", dpi=150, facecolor=background)
        buf.seek(0)
        image_base64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        plt.close(fig)
        return image_base64

    def eachEventGraphList(self, section,event_id):
        SessionLocal = CreateModel().getSession()
        x = []
        y = []
        with SessionLocal() as s:
            tickets = (
                s.query(Ticket)
                .join(Ticket.iteration)
                .join(Iteration.event)
                .filter(
                    Ticket.section == section,
                    Event.id == event_id).all())

            for t in tickets:
                # 1) Iteration for this ticket
                it = t.iteration
                when_captured = it.captured_at

                # 2) Event for this ticket (via iteration)
                ev = t.iteration.event.event_date
                x.append(round((ev - when_captured).total_seconds() / 3600,3))
                y.append(t.price)

            return x, y
