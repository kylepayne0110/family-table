# The household clock: what "today" means for the family, in their own time zone.
# Dinners stay on the planner through the end of their day in this zone, not the server's.

# datetime gives the current moment; date is what the planner compares.
from datetime import date, datetime

# ZoneInfo is the household's time zone.
from zoneinfo import ZoneInfo


class HouseholdClock:
    """Current local time for the household. Tests can freeze it with `frozen`."""

    def __init__(self, zone: ZoneInfo):
        self.zone = zone
        self.frozen: datetime | None = None   # an aware datetime, or None for the real time

    def now(self) -> datetime:
        if self.frozen is not None:
            return self.frozen.astimezone(self.zone)
        return datetime.now(self.zone)

    def today(self) -> date:
        return self.now().date()
