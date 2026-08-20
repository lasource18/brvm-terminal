import pytest

from brvm.sources._num import parse_en_number, parse_fr_number, parse_number

NBSP = "\u00a0"


class TestParseFrNumber:
    def test_nbsp_thousands(self):
        assert parse_fr_number(f"32{NBSP}500") == 32500.0

    def test_space_thousands(self):
        assert parse_fr_number("1 301 997 599") == 1_301_997_599.0

    def test_comma_decimal(self):
        assert parse_fr_number("481,64") == 481.64

    def test_percent_stripped(self):
        assert parse_fr_number("+1,88%") == 1.88

    def test_negative(self):
        assert parse_fr_number("-3,70%") == -3.70

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            parse_fr_number("")

    def test_dash_raises(self):
        with pytest.raises(ValueError):
            parse_fr_number("-")


class TestParseEnNumber:
    def test_comma_thousands(self):
        assert parse_en_number("32,500") == 32500.0

    def test_period_decimal(self):
        assert parse_en_number("1.88%") == 1.88


class TestParseNumber:
    def test_comma_decimal_from_cotation(self):
        assert parse_number("+1,88%") == 1.88

    def test_nbsp_thousands(self):
        assert parse_number(f"3{NBSP}006") == 3006.0

    def test_period_decimal_from_aaz(self):
        # sikafinance aaz page uses period-decimal percentages
        assert parse_number("-3.70%") == -3.70

    def test_nbsp_thousands_with_decimal(self):
        assert parse_number(f"3{NBSP}250{NBSP}000,50") == 3_250_000.50
