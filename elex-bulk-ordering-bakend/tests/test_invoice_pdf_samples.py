"""The owner's second batch of real invoices (26 Oct 2026): eight orders'
zips of four PDFs each, three of which are kept here as fixtures — the
transport (GTA) invoice Flipkart India issues for the delivery, which the
reader did not recognise before; Flipkart Internet's fee invoice with two
lines (Offer Handling Fee and Credit Card Fee), which was labelled by
whichever line came first; and an inter-state product invoice with IGST.
Every field is checked against the PDF."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from app.io.invoice_pdf import ParsedLine, parse_invoice, parse_invoice_text

FIXTURES = Path(__file__).parent / "fixtures" / "invoices" / "OD338918803190451100"


def _read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_the_transport_invoice_is_read_as_a_fee_called_gt_charges():
    """Flipkart India's goods-transport invoice: "Particulars" header, rupee
    signs glued to the numbers, the SAC code in the cell after the words,
    "Billed From / Billed To" instead of "Sold By" / "Bill To", no order date,
    no "Grand Total" line (the Total row's last number stands in)."""
    out = parse_invoice(_read("file_2.pdf"))

    assert out.error is None
    assert (out.kind, out.invoice_type) == ("fee", "GT Charges")
    assert out.invoice_number == "NBABJ27012027019"
    assert out.invoice_date == date(2026, 10, 23)
    assert out.order_id == "OD338918803190451100"
    assert out.order_date is None  # the transport invoice prints none
    assert (out.seller_name, out.seller_gstin) == ("Flipkart India Private Limited", "29AABCF8078M1ZX")
    assert (out.buyer_name, out.buyer_gstin) == ("Sk Enterprises", "09BUAPA6939F1ZP")
    assert out.grand_total == 587.00
    assert out.lines == [
        ParsedLine(
            title="GT Charges",
            fsn=None,
            hsn_sac="996511",
            imei=None,
            qty=1,
            gross=587.00,
            discount=None,
            taxable=497.46,
            cgst=None,
            sgst=None,
            igst=89.54,
            total=587.00,
        ),
    ]


def test_a_fee_invoice_with_two_lines_is_labelled_by_both_in_a_fixed_order():
    """Offer Handling Fee and Credit Card Fee on one Flipkart Internet invoice:
    one label naming both, the same whichever line the PDF prints first, so
    the Extract Invoices Data list shows this document under one name."""
    out = parse_invoice(_read("file_1.pdf"))

    assert out.error is None
    assert (out.kind, out.invoice_type) == ("fee", "Credit Card Fee + Offer Handling Fee")
    assert out.invoice_number == "MXAAB27007778336"
    assert (out.order_date, out.invoice_date) == (date(2026, 10, 18), date(2026, 10, 19))
    assert (out.seller_name, out.seller_gstin) == ("Flipkart Internet Private Limited", "29AACCF0683K1ZD")
    assert (out.buyer_name, out.buyer_gstin) == ("Sk Enterprises", "09BUAPA6939F1ZP")
    assert out.grand_total == 177.00
    assert [(line.title, line.hsn_sac, line.gross, line.taxable, line.igst, line.total) for line in out.lines] == [
        ("Offer Handling Fee", "998599", 129.00, 109.32, 19.68, 129.00),
        ("Credit Card Fee", "998599", 48.00, 40.68, 7.32, 48.00),
    ]
    assert all((line.qty, line.discount, line.cgst, line.sgst) == (1, 0.0, None, None) for line in out.lines)


def test_an_inter_state_product_invoice_reads_igst_and_the_imei():
    out = parse_invoice(_read("file_3.pdf"))

    assert out.error is None
    assert (out.kind, out.invoice_type) == ("product", "Product")
    assert out.invoice_number == "LIBFX4S270000245"
    assert out.order_id == "OD338918803190451100"
    assert (out.order_date, out.invoice_date) == (date(2026, 10, 18), date(2026, 10, 19))
    assert (out.seller_name, out.seller_gstin) == ("hatman retails private limited", "29AAHCH2846P2ZU")
    assert (out.buyer_name, out.buyer_gstin) == ("Sk Enterprises", "09BUAPA6939F1ZP")
    assert out.grand_total == 15162.00
    assert out.lines == [
        ParsedLine(
            title="vivo T5 Lite 44W 5G (Twilight Shadow, 64 GB)",
            fsn="MOBHQSFF6MZK4YNP",
            hsn_sac="85171300",
            imei="864414083556330",
            qty=1,
            gross=16912.00,
            discount=-1750.00,
            taxable=12849.15,
            cgst=None,
            sgst=None,
            igst=2312.85,
            total=15162.00,
        ),
    ]


def test_a_tax_rate_line_without_a_colon_does_not_join_the_title():
    """The transport invoice prints "IGST 18.0 %" under its row (no colon);
    the product and fee invoices print "IGST: 18.0 %". Neither is title text."""
    text = "\n".join(
        [
            "Tax Invoice",
            "Tax Invoice Number : NB1\tNature Of Supply : Service",
            "Invoice Date : 23-10-2026",
            "Order Number : OD111111111111111111",
            "Particulars\tSAC\tQty\tGross\tTaxable\tIGST\tTotal",
            "Amount\tValue",
            "GT Charges\t996511\t1.0\t₹587.00\t₹497.46\t₹89.54\t₹587.00",
            "IGST 18.0 %",
            "Total\t1.0\t₹587.00\t₹497.46\t₹89.54\t₹587.00",
        ]
    )
    out = parse_invoice_text(text)

    assert out.error is None
    assert out.invoice_type == "GT Charges"
    assert [line.title for line in out.lines] == ["GT Charges"]
    assert out.lines[0].hsn_sac == "996511"
    assert out.grand_total == 587.0


def test_the_same_two_fee_lines_in_the_other_order_get_the_same_label():
    def invoice(first: str, second: str) -> str:
        return "\n".join(
            [
                "Tax Invoice",
                "Sold By: Flipkart Internet Private Limited ,",
                "Invoice Number MX1",
                "Order ID: OD111111111111111111\tBilling Address",
                "Order Date: 18-10-2026\tSomeone",
                "Invoice Date: 19-10-2026",
                "Description\tQty\tGross\tDiscount\tTaxable\tIGST ₹\tTotal ₹",
                f"SAC: 998599\t{first}\t1\t129.00\t0.00\t109.32\t19.68\t129.00",
                "IGST: 18.0 %",
                f"SAC: 998599\t{second}\t1\t48.00\t0.00\t40.68\t7.32\t48.00",
                "IGST: 18.0 %",
                "Total\t2\t177.00\t0.00\t150.00\t27.00\t177.00",
                "Grand Total\t₹ 177.00",
            ]
        )

    one = parse_invoice_text(invoice("Offer Handling Fee", "Credit Card Fee"))
    other = parse_invoice_text(invoice("Credit Card Fee", "Offer Handling Fee"))

    assert one.invoice_type == other.invoice_type == "Credit Card Fee + Offer Handling Fee"
    # A single-line fee invoice keeps its plain name.
    single = parse_invoice_text(invoice("Secure Packaging Fee", "Secure Packaging Fee"))
    assert single.invoice_type == "Secure Packaging Fee"
