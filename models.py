
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import JSON
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy import DateTime
from sqlalchemy import create_engine, ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker, relationship
from sqlalchemy import Integer, String
import json


class Base(DeclarativeBase):
    pass

class Event(Base):
    __tablename__ = "event"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(nullable=False)
    event_date: Mapped[datetime] = mapped_column(nullable=False)
    event_sections: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False
    )
    URL : Mapped[str] = mapped_column(nullable=True)
    Place : Mapped[str] = mapped_column(nullable=True)

    iterations: Mapped[list["Iteration"]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan"
    )


class Iteration(Base):
    __tablename__ = "iterations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("event.id"), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(ZoneInfo("America/New_York")), nullable=False
    )

    # backref to parent Event
    event: Mapped[Event] = relationship(back_populates="iterations")

    # Iteration -> many Tickets
    tickets: Mapped[list["Ticket"]] = relationship(
        back_populates="iteration",
        cascade="all, delete-orphan"
    )

class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    section: Mapped[str] = mapped_column(String)
    price: Mapped[int] = mapped_column(Integer)
    #Temporary nullable=True as the first 3185 tickets dont contain this
    ticketsPerSection : Mapped[int] = mapped_column(Integer, nullable=True)

    iteration_id: Mapped[int] = mapped_column(ForeignKey("iterations.id"))
    iteration: Mapped["Iteration"] = relationship(back_populates="tickets")

import os

class CreateModel:
    def __init__(self):
        base_dir = os.path.dirname(os.path.abspath(__file__))  # directory where models.py is
        db_path = os.path.join(base_dir, "Event-collection.db")
        self.engine = create_engine(f"sqlite:///{db_path}", echo=False)
        # Base.metadata.create_all(self.engine) This creates a new file if it cant find it
        if not os.path.exists(db_path):
            raise FileNotFoundError(f"Database file missing: {db_path}")

        self.SessionLocal = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)

    def getSession(self):
        return self.SessionLocal