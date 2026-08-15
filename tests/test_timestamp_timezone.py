"""Timestamps must land in naive LOCAL time, whatever format they arrived in.

Everything downstream — the recency boost, `after`/`before` date filters, and
the epoch values written to SQLite — treats these datetimes as local, which is
what `datetime.fromtimestamp` already returns for epoch inputs. Offset-aware
ISO 8601 strings (Kiro 1.0 and Claude Code both emit "...Z") therefore have to
be converted before the tzinfo is dropped; dropping it first stores the UTC
wall-clock reading as if it were local and shifts the message by the machine's
UTC offset.
"""

from datetime import UTC, datetime, timedelta

import pytest

from kiro_ception.cli_loader import _parse_timestamp as cli_parse
from kiro_ception.ide_loader import _parse_timestamp as ide_parse

PARSERS = [
    pytest.param(ide_parse, id="ide_loader"),
    pytest.param(cli_parse, id="cli_loader"),
]


def local_offset(dt: datetime) -> timedelta:
    """The machine's UTC offset at a given instant."""
    return dt.astimezone().utcoffset()


class TestUtcStringsBecomeLocal:
    @pytest.mark.parametrize("parse", PARSERS)
    def test_z_suffix_converted_to_local(self, parse):
        parsed = parse("2026-06-01T14:30:00Z")
        expected = datetime(2026, 6, 1, 14, 30, tzinfo=UTC).astimezone().replace(tzinfo=None)
        assert parsed == expected

    @pytest.mark.parametrize("parse", PARSERS)
    def test_result_is_naive(self, parse):
        assert parse("2026-06-01T14:30:00Z").tzinfo is None

    @pytest.mark.parametrize("parse", PARSERS)
    def test_explicit_offset_converted_to_local(self, parse):
        # Same instant expressed two ways must land on the same local time.
        assert parse("2026-06-01T14:30:00+00:00") == parse("2026-06-01T14:30:00Z")

    @pytest.mark.parametrize("parse", PARSERS)
    def test_non_utc_offset_converted(self, parse):
        # 09:30-05:00 is the same instant as 14:30Z.
        assert parse("2026-06-01T09:30:00-05:00") == parse("2026-06-01T14:30:00Z")

    @pytest.mark.parametrize("parse", PARSERS)
    def test_naive_string_left_alone(self, parse):
        # No offset means it is already local; shifting it would corrupt it.
        assert parse("2026-06-01T14:30:00") == datetime(2026, 6, 1, 14, 30)

    @pytest.mark.parametrize("parse", PARSERS)
    def test_epoch_millis_still_local(self, parse):
        ts = 1717614000000
        assert parse(ts) == datetime.fromtimestamp(ts / 1000)


class TestSourcesAgreeOnTheSameInstant:
    """A Claude ISO timestamp and a Kiro epoch for the same instant must match.

    They previously disagreed by the machine's UTC offset, which is what made
    `newest_message_at` read 12 hours stale at UTC+12 and made date filters
    drop the first hours of a local day.
    """

    def test_iso_and_epoch_agree(self):
        instant = datetime(2026, 6, 1, 14, 30, tzinfo=UTC)
        from_iso = ide_parse("2026-06-01T14:30:00Z")
        from_epoch = ide_parse(int(instant.timestamp() * 1000))
        assert from_iso == from_epoch

    def test_round_trips_back_to_the_same_instant(self):
        instant = datetime(2026, 6, 1, 14, 30, tzinfo=UTC)
        parsed = ide_parse("2026-06-01T14:30:00Z")
        # Stored as an epoch, it must decode to the original instant.
        assert parsed.timestamp() == pytest.approx(instant.timestamp())

    def test_offset_is_actually_applied_when_machine_is_not_utc(self):
        parsed = ide_parse("2026-06-01T14:30:00Z")
        naive_utc = datetime(2026, 6, 1, 14, 30)
        offset = local_offset(naive_utc)
        if offset == timedelta(0):
            pytest.skip("machine is on UTC; no shift to observe")
        assert parsed != naive_utc
        assert parsed - naive_utc == offset


class TestUnchangedBehaviour:
    @pytest.mark.parametrize("parse", PARSERS)
    def test_none(self, parse):
        assert parse(None) is None

    @pytest.mark.parametrize("parse", PARSERS)
    def test_invalid_string(self, parse):
        assert parse("not-a-date") is None

    @pytest.mark.parametrize("parse", PARSERS)
    def test_zero_epoch(self, parse):
        assert parse(0) == datetime.fromtimestamp(0)
