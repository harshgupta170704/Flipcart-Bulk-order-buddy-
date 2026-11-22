"""All Flipkart selectors in one place.

Carried over verbatim from the legacy Selenium code (login-otp-generator,
add-coupons, flipkart-ordering-tool) so the domain knowledge lives in a single
file. Playwright uses these as ``xpath=...`` locators.

⚠️  Flipkart's CSS class names are obfuscated and change over time. Entries
marked ``# VERIFY`` were NOT present in the legacy code and must be confirmed
against the live site before the flow can place real orders — most importantly
the OTP input and the pre-payment order total used by the price-target gate.
"""

# --- URLs --------------------------------------------------------------------
LOGIN_URL = "https://www.flipkart.com/account/login?ret=/"
REWARDS_URL = "https://www.flipkart.com/myrewards?enableCouponCode=true"
ORDERS_URL = "https://www.flipkart.com/account/orders"

# --- Login (standalone login page) ------------------------------------------
# CONFIRMED against the live page on 2026-10-23. The classes inherited from the
# legacy code ("c3Bd2c yXUQVt", "dSM5Ub Kv3ekh KcXDCU") were already DEAD —
# Flipkart has redeployed since that code was written, which is why these are
# now matched on type and visible text instead of obfuscated class names.
#
# Flipkart now opens on PHONE login. The email field does not exist until the
# "Use Email-ID" link is clicked, so that click is a required first step — the
# legacy flow typed an email into what is currently a number input.
LOGIN_USE_EMAIL = '//*[normalize-space(text())="Use Email-ID"]'
LOGIN_EMAIL = '//input[@type="email"]'
LOGIN_PHONE = '//input[@type="number"]'
# Single "Continue" button; it requests the OTP.
SEND_OTP_BTN = '//button[normalize-space()="Continue"]'

# CONFIRMED live 2026-10-23 (OTP triggered on a real account to see this screen).
# The OTP is SIX separate single-digit inputs, not one field — the guessed
# `//input[@autocomplete="one-time-code"]` matched nothing, and a single
# .fill(code) could never have worked. The Verify button stays DISABLED until
# all six boxes hold a digit, so every box must be filled before clicking.
OTP_DIGIT_BOXES = '//input[@class="S1KmoO"]'
VERIFY_BTN = '//button[normalize-space()="Verify"]'
OTP_RESEND = '//*[normalize-space(text())="Resend code"]'

# --- Buy-now + in-checkout login --------------------------------------------
# CONFIRMED live 2026-10-23: "Buy now" is a <div> (React-Native-Web, class
# "css-g5y9jx"), NOT the <button class="dSM5Ub ugg2XR KCQxN7 KcXDCU"> the legacy
# code looked for — wrong tag AND wrong classes, so that selector was dead.
# Matched on text, and on the innermost node (no element children) so we click
# the label rather than an outer wrapper. Note the casing: "Buy now".
#
# Seen live on 13 Nov 2026: on some products (Pigeon cookers) the button now
# reads "Buy at ₹759", its price drawn digit by digit like an odometer, so its
# text is "Buy at ₹0123456789…" and no single node says "Buy now". The same
# page also has an offers line "Buy at ₹759" near the price, which is NOT the
# button. What tells them apart is the odometer: the button's text carries its
# digit columns ("0123456789"), the offers line never does. The innermost such
# node is the button. Phones still say "Buy now" and match the first form.
_BUY_AT_ODOMETER = 'starts-with(normalize-space(.),"Buy at ₹") and contains(.,"0123456789")'
BUY_NOW_BTN = (
    '(//*[not(*)][normalize-space(text())="Buy now"]'
    f' | //*[{_BUY_AT_ODOMETER}][not(*[{_BUY_AT_ODOMETER}])])[1]'
)
CHECKOUT_EMAIL = '//input[@class="c3Bd2c XnemW5"]'
CHECKOUT_CONTINUE = '//button[@class="dSM5Ub dL4Phl KcXDCU"]'

# --- Address -----------------------------------------------------------------
ADDRESS_ACTION_BTN = '//button[@class="MQGkjR"]'  # list; legacy clicks index [1]
ADD_NEW_ADDRESS = '//button[@class="fONBOF"]'
ADDR_NAME = '//input[@name="name"]'
ADDR_PHONE = '//input[@name="phone"]'
ADDR_PIN = '//input[@name="pincode"]'
ADDR_LINE2 = '//input[@name="addressLine2"]'
ADDR_LINE1 = '//textarea[@name="addressLine1"]'
DELIVER_HERE = '//button[@class="dSM5Ub up3BdN KcXDCU"]'

# --- Quantity ----------------------------------------------------------------
QTY_INPUT = '//input[@class="j93Ywx"]'  # legacy; matched nothing on the live checkout
#: The checkout's quantity control, mapped live on 24 Oct 2026: a clickable
#: box reading "Qty: N" that opens a short list ("1", "2", "more"); "more"
#: opens an "Enter Quantity" dialog with an input and an APPLY button. Only a
#: real pointer click moves it — a synthetic DOM click does nothing — and the
#: box's text is the one reliable read-back of what Flipkart accepted.
QTY_BOX = "//div[contains(@style,'cursor: pointer')][.//div[starts-with(normalize-space(.),'Qty:')]]"
QTY_OPTION = "//div[@dir='auto' and normalize-space(.)='{value}']"  # .format(value=…)
QTY_MORE = "//div[@dir='auto' and normalize-space(.)='more']"
QTY_DIALOG_INPUT = "//input[@placeholder='Quantity']"
QTY_APPLY = "//div[@dir='auto' and normalize-space(.)='APPLY']"

# --- GST ---------------------------------------------------------------------
# The checkout's GST invoice control, mapped live on 27 Oct 2026 on a signed-in
# checkout of the second account (nothing ordered: a ₹1 limit stopped the
# rehearsal before payment). Under the item, a "Use GST Invoice" tick box;
# ticked, it opens a side panel, "Add new GST Details", with a GSTIN box
# (maxlength 15, typed in capitals), a Business Name box (maxlength 60) and
# "Confirm and Save". React Native Web gives none of them a name, role or
# stable class, so they are found by their wording and their limits. The
# legacy tool's class-name buttons this replaced matched nothing any more.
GST_USE_INVOICE_TEXT = "Use GST Invoice"
GST_GSTIN_INPUT = 'input[maxlength="15"][autocapitalize="characters"]'
GST_BUSINESS_INPUT = 'input[maxlength="60"]'
GST_CONFIRM_TEXT = "Confirm and Save"

# --- Order summary -----------------------------------------------------------
# The final payable amount the price-target gate reads (PLAN.md §6a).
#
# CONFIRMED live 2026-10-23 on /viewcheckout. The previous selector,
# `//span[contains(@class,"U0drn_")]`, matched ZERO elements there — so the gate
# read nothing, fell back to 0.00, and 0.00 <= target approved every order. The
# cap was silently inert. This is anchored on the visible "Total Amount" label
# and takes the next ₹ value, which survives class churn.
#
# It must be the TOTAL, not the item price: a cart showing ₹27,999 totalled
# ₹28,128 once the ₹129 Protect Promise Fee was added. Capping on the item
# price would wave through an order that charges more than the target.
ORDER_TOTAL_LABEL = '//*[normalize-space(text())="Total Amount"]'

# Flipkart blocks checkout until the delivery address has an exact map pin.
# CONFIRMED live 2026-10-23: on a gated checkout the page renders NO Continue /
# Place Order / Pay control at all (0 matches across 974 elements) — only a
# yellow "Set Location" button that navigates to a Google Maps pin picker.
# Detecting this up front turns a two-minute cascade of selector timeouts into
# one clear sentence naming the real problem.
CHECKOUT_LOCATION_GATE = (
    '//*[not(*)][contains(normalize-space(text()),"set exact location")'
    ' or contains(normalize-space(text()),"Please set exact location")]'
)
CHECKOUT_SET_LOCATION_BTN = '//*[not(*)][normalize-space(text())="Set Location"]'

#: "Delivery by Oct 28, Wed" on the checkout page (confirmed live 2026-10-23).
#: Text-anchored on purpose — the wording is stable, the class names are not.
CHECKOUT_DELIVERY_BY = (
    '//*[not(*)][starts-with(normalize-space(text()),"Delivery by")'
    ' or starts-with(normalize-space(text()),"Delivery By")]'
)
#: The pre-discount cart figure, for the separate "cart amount" cap. The live
#: checkout showed "₹45,999" against this label while the payable total was
#: ₹28,128 — they are different numbers and must not be conflated.
#: Seen live on 13 Nov 2026: the price block now reads "Selling Price ₹2,999 |
#: Fees ₹19 | Discounts ₹2,200 | Total Amount ₹818", so "Selling Price" is the
#: cart figure's label now; without it every run with a cart cap was refused.
CHECKOUT_CART_AMOUNT = (
    '//*[normalize-space(text())="Price"'
    ' or normalize-space(text())="Selling Price"'
    ' or normalize-space(text())="Cart Amount"]'
    '/following::*[starts-with(normalize-space(text()),"₹")][1]'
)
ORDER_TOTAL = (
    '//*[normalize-space(text())="Total Amount"]'
    '/following::*[starts-with(normalize-space(text()),"₹")][1]'
)

# --- Adding a delivery address (mapped live 2026-10-24) -----------------------
# Flipkart ties every address to a map pin now. From the checkout's address
# picker ("Saved addresses"):
#
#   Add New -> "Away from my location" -> type in the map search -> pick a
#   suggestion -> the pin page shows "Deliver To" + the locality it resolved
#   (with the PINCODE) -> "Add address Details" -> form -> "Save address"
#
# The form's inputs carry no name, id or placeholder. In DOM order they are:
# house/flat (text, empty, required), name (text, prefilled with the account's
# name), phone (tel, prefilled), alternate phone (tel, optional). The pincode
# is NOT typed — it comes from the pin, which is why the pin is checked against
# the address before anything is saved.
ADDRESS_PICKER_HEADING = '//*[not(*)][normalize-space(text())="Saved addresses"]'
ADDRESS_ADD_NEW = '//*[not(*)][normalize-space(text())="Add New"]'
ADDRESS_AWAY_FROM_LOCATION = '//*[not(*)][normalize-space(text())="Away from my location"]'
ADDRESS_MAP_SEARCH = '//input[@placeholder="Search by area, name, street."]'
ADDRESS_PIN_DELIVER_TO = '//*[not(*)][normalize-space(text())="Deliver To"]'
ADDRESS_ADD_DETAILS = '//*[not(*)][normalize-space(text())="Add address Details"]'
ADDRESS_FORM_TEXT_INPUTS = '//input[@type="text" and not(@name="q") and not(@placeholder)]'
ADDRESS_FORM_TEL_INPUTS = '//input[@type="tel"]'
ADDRESS_TYPE_HOME = '//*[not(*)][normalize-space(text())="Home"]'
ADDRESS_TYPE_WORK = '//*[not(*)][normalize-space(text())="Work"]'
ADDRESS_SAVE = '//*[not(*)][normalize-space(text())="Save address"]'
ADDRESS_MAP_PATH = "/address-map/"
#: The amount written on the final CTA itself — "Pay ₹16,105".
#:
#: CONFIRMED live 2026-10-24: on a prepaid payments page this and "Total Amount"
#: both read ₹16,105, but they are SEPARATE elements that disagree while the
#: page re-prices, so the gate reads both and caps on the larger. A COD CTA says
#: "Place Order" and carries no amount at all, so its absence is normal and must
#: not be treated as a read failure.
PAY_CTA_AMOUNT = '//button[starts-with(normalize-space(.),"Pay ₹")]'

# --- The "Add to cart" path (mapped live 2026-10-23) -------------------------
# Not every product offers "Buy now". A women's top offered ONLY "Add to cart",
# and offered nothing at all until a SIZE was picked — so a flow built purely on
# Buy now simply times out on whole categories of product.
#
#   pick size (apparel) -> "Add to cart" -> "Go to cart" -> /viewcart -> "Place Order"
#: An element's own text lower-cased, for matching Flipkart's wording whatever
#: its capitals ("Go to cart" / "GO TO CART", "Out for Delivery" / "delivery").
_LOWER_TEXT = 'translate(normalize-space(text()),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")'

CART_URL = "https://www.flipkart.com/viewcart"
#: The cart page's path, to tell "landed on the cart" from anywhere else.
CART_PATH = "/viewcart"
ADD_TO_CART_BTN = '//*[not(*)][normalize-space(text())="Add to cart"]'
#: What "Add to cart" turns into once the product is in the cart: Flipkart's
#: own proof that the add took (seen live 26 Oct 2026).
GO_TO_CART_BTN = f'//*[not(*)][{_LOWER_TEXT}="go to cart"]'
PLACE_ORDER_BTN = '//*[not(*)][normalize-space(text())="Place Order"]'
#: Per-item link in the cart. Used to EMPTY the cart before adding anything:
#: "Place Order" buys the WHOLE cart, so a stray item from a previous session
#: would be purchased alongside the intended product.
#: "Saved For Later (25)" heads the list of saved items on the cart page, and
#: each saved row carries a "Remove" link too (seen live 26 Oct 2026 on an
#: account with 25 saved items and an empty cart).
CART_SAVED_HEADING = f'//*[not(*)][starts-with({_LOWER_TEXT},"saved for later")]'
#: A cart line's "Remove" — never a saved item's: only the links that come
#: BEFORE the "Saved For Later" heading. Emptying the cart used to press the
#: first "Remove" anywhere, which on an empty cart is a saved item's.
CART_REMOVE_ITEM = (
    f'//*[not(*)][{_LOWER_TEXT}="remove"]'
    f'[not(preceding::*[not(*)][starts-with({_LOWER_TEXT},"saved for later")])]'
)
#: The confirmation box's own "Remove", found from its "Cancel": the nearest
#: box around "Cancel" that holds a "Remove" — and never one that also holds
#: the saved list, so the page itself can never pass for the box.
CART_REMOVE_CONFIRM = (
    f'//*[not(*)][{_LOWER_TEXT}="cancel"]'
    f'/ancestor::*[.//*[not(*)][{_LOWER_TEXT}="remove"]][1]'
    f'[not(.//*[not(*)][starts-with({_LOWER_TEXT},"saved for later")])]'
    f'//*[not(*)][{_LOWER_TEXT}="remove"]'
)
#: The empty cart's own heading, "Your cart is empty!" (seen live 25 Oct 2026).
CART_EMPTY_TEXT = f'//*[not(*)][contains({_LOWER_TEXT},"your cart is empty")]'
CART_ITEM_COUNT = '//*[not(*)][starts-with(normalize-space(text()),"Flipkart (")]'

#: Apparel size chips. The product offers no buy control until one is chosen.
SIZE_CHIP = '//*[not(*)][normalize-space(text())=%s]'

# --- Choosing the seller (mapped live 26 Oct 2026, signed out, read only) ----
# Every account bought from whichever seller the product page shows by default,
# and the orders placed were cancelled "as per seller policy restrictions
# applicable to this brand" — a seller's decision. So a run may name a seller:
#
#   product page "Seller: X" + "See other sellers" (href /sellers?pid=<PID>)
#   -> /sellers?pid=<PID>: "All Sellers", columns Seller / Price / Delivery,
#      one row per seller: name (a span), rating, "Cash On Delivery available",
#      price, offers, "Usually delivered in 3 days", ADD TO CART, BUY NOW
#
# The rows carry obfuscated classes only (div.QGdlvi), so everything here is
# matched on text. The two buttons are real <button>s whose words Flipkart's
# CSS upper-cases: the DOM says "Add to cart" and "Buy Now".
#: The product page's "See other sellers" link. Absent when one seller has it.
OTHER_SELLERS_LINK = (
    '//a[contains(@href,"/sellers?") or translate(normalize-space(.),'
    '"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="see other sellers"]'
)
#: The page listing every seller of one product, as ``?pid=<PID>``.
SELLERS_URL = "https://www.flipkart.com/sellers"
#: A button's whole text, upper-cased: "Buy Now" and "BUY NOW" read the same.
_UPPER_BUTTON_TEXT = 'translate(normalize-space(.),"abcdefghijklmnopqrstuvwxyz","ABCDEFGHIJKLMNOPQRSTUVWXYZ")'
SELLER_BUY_NOW_TEXT = f'{_UPPER_BUTTON_TEXT}="BUY NOW"'
SELLER_ADD_TO_CART_TEXT = f'{_UPPER_BUTTON_TEXT}="ADD TO CART"'
_SELLER_BUTTON = f"button[{SELLER_BUY_NOW_TEXT} or {SELLER_ADD_TO_CART_TEXT}]"
#: Any row's buy control on the sellers page; the rows have arrived once one shows.
SELLER_ROW_BUTTON = f"//{_SELLER_BUTTON}"
#: An element whose own text, lower-cased with its spaces collapsed, is a
#: seller's name. ``{name}`` is that lower-cased name as an XPath literal,
#: built by ``app.automation.sellers.xpath_literal`` (a name can hold quotes).
SELLER_NAME_TEST = f"{_LOWER_TEXT}={{name}}"
#: The chosen seller's name in its row — with a row's button after it, which
#: leaves out the menus above the list and the footer below it.
SELLER_NAME = f"//*[{SELLER_NAME_TEST}][following::{_SELLER_BUTTON}]"
#: The chosen seller's row: the deepest element around the name that holds a
#: row button of EITHER kind and none of the other listed sellers' names
#: (``{others}`` is ``[not(.//*[<name test> or …])]``, or blank).
#:
#: Either kind, not BUY NOW alone as first mapped: a row offering only ADD TO
#: CART holds no BUY NOW, so "the deepest element holding a BUY NOW" is then
#: the whole list, and its BUY NOW is another seller's. On a page in the live
#: row shape that matched six other rows' buttons — and beside one other
#: seller it would match exactly one, the wrong one. The other names close the
#: same gap for a row with no button at all.
SELLER_ROW = f"({SELLER_NAME}/ancestor::*[.//{_SELLER_BUTTON}]{{others}})[last()]"
#: An element whose own text is a plain rupee figure and nothing more: a
#: row's price ("₹749", or the struck-out "₹1,305"). An offer that starts
#: with a rupee figure ("₹100 Cashback on …", "₹721/month") says more than
#: that, so it never counts as one.
_PLAIN_RUPEE_FIGURE = (
    'starts-with(normalize-space(text()),"₹")'
    ' and translate(normalize-space(substring-after(normalize-space(text()),"₹")),"0123456789,.","")=""'
)
#: One control of that row, looked for inside it only. ``{control}`` is
#: SELLER_BUY_NOW_TEXT or SELLER_ADD_TO_CART_TEXT. The caller counts the
#: matches and presses only when there is exactly one.
#:
#: The control is also tied to its own row from the button's side: the
#: nearest element around it that holds a price must hold the chosen name
#: too. The row above is found from the name's side, and the other names
#: keep it apart only when the list names every seller. It does not when a
#: row with no buttons, no price and no cash-on-delivery line is followed by
#: one whose rating line is missing or in a form never mapped: the page's
#: text then reads the two as one row (the first seller's, with the second's
#: price and BUY NOW) and the second seller goes unlisted. Beside just that
#: one other seller, the row found from the name is the whole list, and its
#: one BUY NOW is the other seller's.
SELLER_ROW_CONTROL = (
    f"{SELLER_ROW}//button[{{control}}]"
    f"[ancestor::*[.//*[{_PLAIN_RUPEE_FIGURE}]][1][.//*[{SELLER_NAME_TEST}]]]"
)

# --- Choosing the delivery address (mapped live 2026-10-24) ------------------
# Flipkart pre-selects ONE saved address per account, and it is frequently not
# the one the operator wants: the Ayush account holds 24 saved addresses with a
# Hardoi one selected while the intended destination was Lucknow. Shipping to
# the wrong address succeeds silently, so this has to be chosen explicitly.
#
# The picker has no radios and no "Deliver Here" button — clicking the address
# ROW selects it, and the current one carries a "Selected" marker.
CHECKOUT_CHANGE_ADDRESS = '//*[not(*)][normalize-space(text())="Change"]'
ADDRESS_PICKER_HEADER = '//*[not(*)][normalize-space(text())="Select delivery address"]'
ADDRESS_PICKER_ROW = '//*[not(*)][contains(@class,"r-dnmrzs")]'
ADDRESS_SELECTED_MARK = '//*[not(*)][normalize-space(text())="Selected"]'
#: The line on the checkout page showing where the order will actually go.
DELIVER_TO_LINE = '//*[not(*)][normalize-space(text())="Deliver to:"]'

# --- The REAL checkout flow, mapped live 2026-10-23 --------------------------
# product page -> "Buy now" -> /viewcheckout -> "Continue" -> /payments
#
# What the legacy code assumed, and why every step timed out:
#   * it looked for <button> elements. /viewcheckout has NONE — it is
#     React-Native-Web <div>s, so //button[@class=...] could never match.
#   * it tried to ADD an address. Flipkart already has one selected and only
#     offers a "Change" link, so //input[@name="name"] never appears.
#   * the payment page is a SEPARATE url (/payments), reached via Continue.
PAYMENTS_PATH = "/payments"

#: Yellow CTA on /viewcheckout that goes to the payment page.
CHECKOUT_CONTINUE_CTA = '//*[not(*)][normalize-space(text())="Continue"]'

#: Payment methods are spans carrying the visible label.
PAY_OPTION = '//span[contains(@class,"QbbOLN")][normalize-space(.)=%s]'
PAY_OPTION_CARD = PAY_OPTION % '"Credit / Debit / ATM Card"'
PAY_OPTION_COD = PAY_OPTION % '"Cash on Delivery"'

# The payment page's balance toggles: a row that names SuperCoins (or the gift
# card / wallet balance) and carries a checkbox or a switch. Matched on the
# wording, any case, so a change of markup that keeps the words keeps working.
# NOT YET SEEN LIVE: no account had SuperCoins or a gift card balance when this
# was written (17 Nov 2026), so Flipkart did not draw the rows. A run that asks
# for them and finds none says so on the order and carries on without them.
_TOGGLE_ROW = (
    "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), %s)]"
    "[.//input[@type='checkbox'] or .//*[@role='checkbox' or @role='switch']]"
    "[not(.//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), %s)]"
    "[.//input[@type='checkbox'] or .//*[@role='checkbox' or @role='switch']])]"
)
SUPERCOINS_ROW = _TOGGLE_ROW % ("'supercoin'", "'supercoin'")
GIFT_CARD_ROW = _TOGGLE_ROW % ("'gift card'", "'gift card'")
PAY_OPTION_UPI = PAY_OPTION % '"UPI"'

# --- Gift cards on the payment page (mapped live 17 Nov 2026) ---------------
# "Have a Flipkart Gift Card?" opens a form: "Voucher Number" and "Voucher
# PIN" (number inputs, placeholders below) and "Add Gift Card"; up to 15 gift
# cards per transaction.
GIFT_CARD_OPEN = '//*[not(*)][normalize-space(text())="Have a Flipkart Gift Card?"]'
GIFT_CARD_NUMBER = '//input[@placeholder="Enter voucher number"]'
GIFT_CARD_PIN = '//input[@placeholder="Enter voucher PIN"]'
GIFT_CARD_ADD = '//*[self::button or @role="button"][normalize-space(.)="Add Gift Card"]'
#: Most gift cards one payment takes.
GIFT_CARDS_PER_PAYMENT = 15

# The payment options the order form names but a consumer account's payment
# page did not show on 17 Nov 2026 (UPI, Card, Cash on Delivery, Gift Card
# and EMI only). Matched ONLY as a payment-option label (PAY_OPTION: the
# method list's own spans, whole label) — never as any text on the page, which
# a note such as "refunds reach your bank by IMPS" would match (review of
# 17 Nov 2026). The API refuses these modes until they are mapped live
# (app.api.jobs.UNMAPPED_PAYMENT_MODES); this is for that day.
PAY_OPTION_LABELS = {
    "cod_card": ("Cash on Delivery with advance", "COD with advance", "Pay advance", "Advance payment"),
    "rtgs": ("RTGS",),
    "imps": ("IMPS",),
}
PAY_OPTION_NETBANKING = PAY_OPTION % '"Net Banking"'
#: An unavailable method carries this extra class — COD read "Unavailable" on a
#: phone, so a COD run must detect that rather than click a dead option.
PAY_OPTION_DISABLED_CLASS = "iUnLoA"
PAY_OPTION_COD_DISABLED = (
    '//span[contains(@class,"QbbOLN") and contains(@class,"iUnLoA")]'
    '[normalize-space(.)="Cash on Delivery"]'
)

# --- Submitting the order (mapped live 2026-10-24) ---------------------------
# Cash on Delivery takes TWO clicks, and only the second one orders anything.
#
# "Place Order" does not place the order. Flipkart's handler for it opens a
# "Confirm Cash on Delivery Order" popup (server-sent content of type
# COD_CONFIRMATION_BOTTOMSHEET) and hands the real order call to the popup's
# "Confirm order" button. Confirmed with the browser's network sealed: pressing
# Place Order sent no order request at all, only the popup's banner image and
# analytics.
#
# The old flow stopped after the first click and navigated away, discarding the
# popup, so not one scripted COD order was ever placed. Its catch-all selectors
# were also unsafe now that a popup sits on top: a "Confirm" prefix matches the
# popup's button as well as anything else starting with that word, and the div
# form matched the button's own inner text, so a retry could press twice.
COD_PLACE_ORDER_BTN = '//button[@id="cod-place-order"]'
#: The popup's primary button. A plain <button> with no id or test id, and its
#: label is server-sent ("Confirm order"), so it is matched on the whole label,
#: ignoring case. Never a prefix: "Cancel" sits right beside it, and Place Order
#: stays in the DOM underneath the popup.
COD_CONFIRM_ORDER_BTN = (
    '//button[translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ",'
    '"abcdefghijklmnopqrstuvwxyz")="confirm order"]'
)
#: A card order has a single submit, labelled with the amount ("Pay ₹16,105").
CARD_PAY_BTN = PAY_CTA_AMOUNT
#: Between Pay and the bank, the payments page may ask whether to save
#: ("secure") the card on the account under the RBI's tokenisation rules: a
#: dialog titled "Secure your card as per RBI guidelines" with "Maybe later"
#: and "Yes, secure my card". Seen in the card run of 27 Oct 2026 (18:35 IST):
#: Pay was pressed, the dialog stood, and nothing went to the bank for 90 s.
#: The run answers "Maybe later", so a corporate card is not left saved on the
#: account. Matched on the words alone: the innermost element holding them.
CARD_SAVE_PROMPT = '//*[not(*)][normalize-space(text())="Secure your card as per RBI guidelines"]'
CARD_SAVE_LATER = '//*[not(*)][normalize-space(text())="Maybe later"]'

#: Where Flipkart lands once an order exists: its OrderConfirmation page, which
#: reads ``?reference_id=`` from the address bar. Flipkart's own page check
#: accepts either path. This page is the only proof of an order the flow accepts.
#:
#: The reference is the order id WITHOUT its last two digits: Flipkart's own
#: tracking code rebuilds the id as ``reference_id + "00"``.
ORDER_CONFIRMATION_PATHS = ("/orderresponse", "/orderConfirmation")
#: The whole page, for reading an order id off the confirmation page.
PAGE_BODY = "//body"

# --- Payment (legacy; card form below still valid) -----------------------------------------------------------------
PAY_CONTINUE_1 = '//button[@class="dSM5Ub tzUB8K KcXDCU"]'
PAY_CONTINUE_2 = '//button[@class="dSM5Ub ZjNM9b KtnAdx"]'

# Cash on Delivery. Matched on LABEL TEXT rather than a class, deliberately:
# Flipkart's class names are obfuscated and churn between deploys, while the
# visible payment-option wording is comparatively stable. `text()` (not `.`)
# keeps this to the element that actually holds the words, instead of every
# ancestor that contains them; `[last()]` then takes the innermost match.
#
# ⚠️ STILL VERIFY, and note the trap found live on 2026-10-23: the PRODUCT page
# carries an informational "Cash on Delivery" badge (an <a> in the highlights
# grid) whose text matches this selector exactly. Clicking that opens an info
# link instead of choosing a payment method. So this selector MUST only ever be
# used on the payment step, and should be re-scoped to a payment-page container
# once that page has been inspected. The real text also contains a line break
# ("Cash on\nDelivery"), which normalize-space() handles — do not switch to a
# plain `=` comparison on the raw text.
PAY_COD_OPTION = '(//*[contains(normalize-space(text()), "Cash on Delivery")])[last()]'
# Shown when COD is not offered for this product/pincode/cart value.
PAY_COD_UNAVAILABLE = (
    '(//*[contains(normalize-space(text()), "not available") or '
    'contains(normalize-space(text()), "Not Available")])[last()]'
)  # VERIFY
PAY_ADD_CARD = "//span[@class='EtFGuU font-m-semibold']"  # legacy; no longer clicked (see PAY_ADD_NEW_CARD)
#: The link that opens a new-card form when an account has saved cards. Matched
#: on its whole wording, never a class: an obfuscated class can sit on any
#: text, the Pay button's included. Clicked only when the card box is hidden.
#: Not seen live yet (the second account shows the form straight away).
PAY_ADD_NEW_CARD = (
    '//*[translate(normalize-space(text()),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")'
    '="add new card" or translate(normalize-space(text()),"ABCDEFGHIJKLMNOPQRSTUVWXYZ",'
    '"abcdefghijklmnopqrstuvwxyz")="+ add new card" or translate(normalize-space(text()),'
    '"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")="add a new card" or '
    'translate(normalize-space(text()),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")'
    '="use a new card"]'
)
CARD_NUMBER = '//input[@id="cc-input"]'
CARD_EXPIRY = '//input[@placeholder="MM / YY"]'
CARD_CVV = '//input[@id="cvv-input"]'
CARD_SUBMIT = (
    '//button[@class="Button-module_button__P7hTI font-m-semibold '
    'Button-module_neutral__OtiH- L13GJr font-m-semibold"]'
)
PAY_CONFIRM = (
    '//button[@class="Button-module_button__P7hTI font-m-semibold '
    'Button-module_secondary__ieoHb HdbLQH font-m-semibold"]'
)

# --- Order details (orders page, after placing) ------------------------------
# CONFIRMED live 2026-10-23, signed in, on BOTH pages — and the distinction is
# the whole bug:
#
#   /account/orders   (the LIST)   — rows only. awBAe9 / JfotKm / eDn_Ye /
#                                    U0drn_ all match ZERO elements here.
#   /order_details    (ONE order)  — all four match exactly 1 element each.
#
# The legacy selectors were never wrong; get_order_details was reading them on
# the list page, where they cannot match. So every placed order was recorded
# with a blank model, colour, amount and id. The fix is to open the order's own
# page first, not to replace the selectors.
ORDER_PLACED_MARKER = '//button[@class="l4zhnI"]'  # VERIFY (confirmation page)

#: One order row. The whole row is the anchor, so every field below is read
#: relative to it rather than from the page, which keeps rows from bleeding
#: into each other when several orders are listed.
ORDER_ROW = '//a[contains(@class,"kok32b")]'
ORDERS_LINK = ORDER_ROW  # legacy name, same element

ORDER_MODEL_IN_ROW = './/span[contains(@class,"G6wtLH")]'
ORDER_AMOUNT_IN_ROW = './/div[contains(@class,"QF9IHY")]'
#: "Delivered on Oct 31, 2018" / "Arriving ..." — status and date in one string.
ORDER_STATUS_IN_ROW = './/span[contains(@class,"sNKed5")]'
ORDER_NOTE_IN_ROW = './/div[contains(@class,"Y8vNIs")]'
#: Attributes are label/value pairs ("Color:" -> "Diamond Blue", "Size:" -> "XL"),
#: so colour is the value that FOLLOWS the label reading "Color:".
ORDER_ATTR_LABEL_IN_ROW = './/span[contains(@class,"Oz6eQV")]'
ORDER_ATTR_VALUE_IN_ROW = './/span[contains(@class,"S2I3v5")]'
# following-SIBLING, not following: the `following::` axis walks out of the row
# and keeps going down the document, so a row whose "Color:" label had no value
# would silently borrow the NEXT order's colour. Sibling keeps it inside the
# label's own pair, and matches nothing when the value is genuinely absent.
ORDER_COLOR_IN_ROW = (
    './/span[contains(@class,"Oz6eQV") and starts-with(normalize-space(.),"Color")]'
    '/following-sibling::span[contains(@class,"S2I3v5")][1]'
)

# --- Order DETAIL page (/order_details) — all CONFIRMED live 2026-10-23 ------
# These are the legacy selectors, verified to match exactly one element each on
# an order's own page. Use them only after navigating there.
ORDER_ID = '//div[@class="awBAe9"]'
ORDER_MODEL = '//div[@class="JfotKm"]'
ORDER_COLOR = '//div[@class="eDn_Ye"]'
ORDER_AMOUNT = '//span[@class="U0drn_"]'
ORDER_BLOCK = '//div[@class="bLYZip zEfFJL fQAJYH Kjci4b"]'
#: "Seller: Flashtech Retail"
ORDER_SELLER = '//div[contains(@class,"eIot62")]'
#: Status steps: "Order Confirmed", "Shipped", "Delivered", ...
#: Matched whatever the case: Flipkart writes "Out for Delivery" in one place
#: and "Out for delivery" in another. "Order Received" heads a cancelled
#: order's timeline (seen live 25 Oct 2026).
ORDER_TIMELINE_STEPS = (
    "//*[not(*)]["
    + " or ".join(
        f'starts-with({_LOWER_TEXT},"{step}")'
        for step in ("order confirmed", "order received", "shipped", "out for delivery", "delivered", "cancelled")
    )
    + "]"
)

#: The order page folds the address under this heading; a click reveals the
#: type ("Work"), the address line, the name and the phone, one per line
#: (seen on a delivered order, 25 Oct 2026). "Price details" follows it.
DELIVERY_DETAILS_HEADING = '//*[not(*)][normalize-space(text())="Delivery details"]'

#: An order's own page carries order_id, item_id and unit_id in its URL — the
#: same three ids the operator's existing portal reports per order unit.
ORDER_DETAIL_PATH = "/order_details"

# --- Coupons (mobile emulation) ---------------------------------------------
COUPON_INPUT = (
    '//input[@class="css-11aywtz r-6taxm2 r-z2wwpe r-rs99b7 r-13awgt0 '
    'r-16y2uox r-ubezar r-135wba7 r-iphfwy r-kls4rr r-1hfyk0a r-1h8ys4a r-fdjqy7"]'
)
COUPON_CLAIM = (
    '//div[@class="css-175oi2r r-1awozwy r-18u37iz r-1777fci '
    'r-1tuna9m r-eu3ka"]'
)
COUPON_SUCCESS = (
    '//div[@class="css-1rynq56 r-1et8rh5 r-1enofrn r-1cwl3u0 r-c8eef1 '
    'r-19u6a5r r-1b7u577 r-1dpkw9"]'
)
COUPON_ALREADY_USED = (
    '//div[@class="css-1rynq56 r-1xv17tt r-1et8rh5 r-1b43r93 r-14gqq1x"]'
)
