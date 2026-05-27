from sqlalchemy import select

from models import Event, Iteration


def deleteIteration(session, Iid: int):
    ev = session.execute(
        select(Iteration).where(Iteration.id == Iid)
    ).scalar_one_or_none()
    if not ev:
        return False
    session.delete(ev)  # cascades to iterations → tickets
    session.commit()
    return True

def deleteEvent(session, Iid: int):
    ev = session.execute(
        select(Event).where(Event.id == Iid)
    ).scalar_one_or_none()
    if not ev:
        return False
    session.delete(ev)  # cascades to iterations → tickets
    session.commit()
    return True