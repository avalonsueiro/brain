#!/usr/bin/env python3
"""Read your calendar. Read-only -- this cannot create, move, or decline.

    gcal.py today
    gcal.py next                       # the next thing, and how long you have
    gcal.py week
    gcal.py range --from 2026-09-20 --to 2026-09-27
    gcal.py calendars

All times render in RIG_TZ, because an agent reasoning about "this afternoon"
off a UTC clock is how you get a reminder at 3am.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auth  # noqa: E402

API = "https://www.googleapis.com/calendar/v3"


def tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("RIG_TZ", "America/New_York"))


def _parse(when: dict) -> tuple[datetime | None, bool]:
    """(start, all_day). Google sends `date` for all-day, `dateTime` otherwise."""
    if when.get("dateTime"):
        return datetime.fromisoformat(when["dateTime"]).astimezone(tz()), False
    if when.get("date"):
        return datetime.fromisoformat(when["date"]).replace(tzinfo=tz()), True
    return None, False


def events(start: datetime, end: datetime, calendar: str = "primary") -> list[dict]:
    payload = auth.api_get(f"{API}/calendars/{calendar}/events", {
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        # singleEvents expands recurrence -- without it a weekly standup comes
        # back as one master event and today's instance is invisible.
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": 100,
    })
    return payload.get("items", [])


def show(items: list[dict], header: str) -> int:
    print(header)
    if not items:
        print("  nothing scheduled")
        return 0
    last_day = None
    for item in items:
        start, all_day = _parse(item.get("start") or {})
        end, _ = _parse(item.get("end") or {})
        if start is None:
            continue
        day = start.strftime("%a %b %d")
        if day != last_day:
            print(f"\n  {day}")
            last_day = day
        when = "all day" if all_day else (
            f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}" if end
            else start.strftime("%H:%M")
        )
        title = item.get("summary") or "(no title)"
        where = item.get("location") or ""
        # Whether you actually said yes is the part you scan for.
        mine = next((a for a in item.get("attendees", []) if a.get("self")), None)
        status = {"accepted": "", "tentative": " [maybe]",
                  "declined": " [declined]",
                  "needsAction": " [unanswered]"}.get(
                      (mine or {}).get("responseStatus", ""), "")
        line = f"    {when:<13} {title}{status}"
        if where:
            line += f"  ·  {where[:40]}"
        print(line)
        if item.get("hangoutLink"):
            print(f"    {'':<13} {item['hangoutLink']}")
    print(f"\n  {len(items)} event(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gcal", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("today")
    sub.add_parser("next", help="the next event and time until it")
    sub.add_parser("week", help="the next 7 days")
    sub.add_parser("calendars", help="which calendars this account can see")
    p = sub.add_parser("range")
    p.add_argument("--from", dest="start", required=True, metavar="YYYY-MM-DD")
    p.add_argument("--to", dest="end", required=True, metavar="YYYY-MM-DD")

    args = parser.parse_args(argv)
    now = datetime.now(tz())

    try:
        if args.cmd == "today":
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            return show(events(midnight, midnight + timedelta(days=1)),
                        f"Today — {now.strftime('%A %B %d')}")

        if args.cmd == "week":
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            return show(events(midnight, midnight + timedelta(days=7)), "Next 7 days")

        if args.cmd == "next":
            upcoming = events(now, now + timedelta(days=14))
            if not upcoming:
                print("nothing scheduled in the next two weeks")
                return 0
            item = upcoming[0]
            start, all_day = _parse(item.get("start") or {})
            delta = start - now
            hours, rem = divmod(max(0, int(delta.total_seconds())), 3600)
            away = (f"{hours // 24}d" if hours >= 24 else
                    f"{hours}h{rem // 60:02d}m" if hours else f"{rem // 60}m")
            print(f"{item.get('summary') or '(no title)'}")
            print(f"  {'all day' if all_day else start.strftime('%a %b %d, %H:%M')}"
                  f"  ({away} from now)")
            if item.get("location"):
                print(f"  {item['location']}")
            if item.get("hangoutLink"):
                print(f"  {item['hangoutLink']}")
            return 0

        if args.cmd == "range":
            start = datetime.fromisoformat(args.start).replace(tzinfo=tz())
            end = datetime.fromisoformat(args.end).replace(tzinfo=tz())
            return show(events(start, end), f"{args.start} → {args.end}")

        if args.cmd == "calendars":
            for cal in auth.api_get(f"{API}/users/me/calendarList").get("items", []):
                mark = "*" if cal.get("primary") else " "
                print(f"{mark} {cal.get('summary')}  ({cal.get('id')})")
            return 0

    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: bad date — {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
