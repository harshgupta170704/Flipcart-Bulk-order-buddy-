"""The two cell guards every export runs free text and identifiers through."""

from app.io.orders_csv import identifier_cell, text_guard


def test_free_text_that_starts_like_a_formula_is_defused():
    """Product names, seller names and messages come from Flipkart or a user;
    a spreadsheet would run a leading "=" as a formula (audit F-36)."""
    assert text_guard("=HYPERLINK(\"http://x\")") == ' =HYPERLINK("http://x")'
    assert text_guard("+1 vivo") == " +1 vivo"
    assert text_guard("-discounted-") == " -discounted-"
    assert text_guard("@seller") == " @seller"
    assert text_guard("vivo T5 Lite 44W 5G") == "vivo T5 Lite 44W 5G"
    assert text_guard(None) == ""


def test_long_identifiers_stay_text_and_short_codes_keep_their_zeros():
    assert text_guard("338975799606623100000") == '="338975799606623100000"'
    # A four-digit delivery OTP keeps its leading zero (audit F-37).
    assert identifier_cell("0123") == '="0123"'
    assert identifier_cell(None) == ""
