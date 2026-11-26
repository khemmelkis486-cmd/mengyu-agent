"""The time zone shared by life scheduling, contact policy and fact evidence."""

import os
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("MENGYU_TIMEZONE") or "UTC")
