import { useCallback, useEffect, useId, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { accountsByEmails, errorMessage, getJob, listCardFailureCodes, listOrderPhones, previewProduct, startOrderJob } from "../lib/api";
import { countLabel, formatBytes, formatCount } from "../lib/format";
import type {
  AccountSelection,
  Address,
  Gst,
  Job,
  PaymentMode,
  ProductPreview as ProductPreviewData,
  QtyMode,
  RunMode,
} from "../lib/types";
import { MAX_CARD_FILE_BYTES, MAX_SELLER_CHARS, OFFERED_PAYMENT_MODES, PAYMENT_MODE_LABELS, selectionCount, usesCard, usesGiftCards } from "../lib/types";
import { AddressGstPicker, type AddressGstChoice } from "./AddressGstPicker";
import { CardPairingPreview } from "./CardPairingPreview";
import { Alert } from "./Alert";
import { Button } from "./Button";
import { Checkbox, Field, FieldLookProvider, FileInput, NumberInput, Select, TextInput } from "./Field";
import { LimitUsageSummary } from "./LimitUsageSummary";
import { OrderPreviewPanel } from "./OrderPreviewPanel";
import { ProductPreview, sameSeller, sellerSummary } from "./ProductPreview";
import { ProductSearch } from "./ProductSearch";
import {
  describePlan,
  EMPTY_QUANTITY,
  orderPlan,
  QuantityFields,
  validateQuantity,
  type QuantityDraft,
} from "./QuantityFields";
import { RunAccountPicker, type RunAccountsState } from "./RunAccounts";

export interface OrderFormProps {
  /** Shared account state — the picker frames its own loading / error states. */
  accounts: RunAccountsState;
  /** True while a run is in flight — the form stays locked until it ends. */
  runActive: boolean;
  /** Called with the job the server created, so the page can stream it. */
  onStarted: (job: Job) => void;
  className?: string;
}

interface FormErrors {
  gift_cards?: string;
  extra_links?: string;
  ai?: string;
  card_rules?: string;
  address_phones?: string;
  exclude_days?: string;
  product_link?: string;
  target?: string;
  cards_file?: string;
  seller?: string;
}

const LINK_STYLE = "font-medium text-accent underline underline-offset-2";

/**
 * The link is typed or pasted, and reading a preview costs the server a real
 * browser page, so the typing is given time to settle first.
 */
const PREVIEW_DEBOUNCE_MS = 600;

/** The delivery phone numbers typed, split on spaces, commas or semicolons. */
/** Every email address in a CSV or text file, in order, without repeats. */
export function emailsInText(text: string): string[] {
  const found = text.match(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g) ?? [];
  return [...new Set(found.map((email) => email.toLowerCase()))];
}

/** Coupon codes or reward ids typed in one box, split on spaces, commas or semicolons. */
export function splitList(text: string): string[] {
  return [...new Set(text.split(/[\s,;]+/).filter(Boolean))];
}

export function splitPhones(text: string): string[] {
  return text.split(/[\s,;]+/).filter((part) => part !== "");
}

/** An optional numeric field: blank or unreadable means "not set", not zero. */
function numberOrNull(text: string): number | null {
  const trimmed = text.trim();
  if (trimmed === "") return null;
  const value = Number(trimmed);
  return Number.isFinite(value) ? value : null;
}

function urlIsUsable(value: string): boolean {
  const trimmed = value.trim();
  if (trimmed === "") return false;
  try {
    const url = new URL(trimmed);
    return (url.protocol === "https:" || url.protocol === "http:") && url.hostname.includes(".");
  } catch {
    return false;
  }
}

/** The card sheet: an .xlsx workbook, or the CSV the owner keeps it as. */
function looksLikeCardSheet(file: File): boolean {
  const name = file.name.toLowerCase();
  return name.endsWith(".xlsx") || name.endsWith(".csv");
}

/** Why a picked file cannot go as the card sheet; null when it can. Such a
 *  file is never sent, not for the pairing preview and not on Start. */
function cardSheetProblem(file: File): string | null {
  if (!looksLikeCardSheet(file)) return "That is not an .xlsx or .csv file. Upload the corporate-card sheet.";
  if (file.size > MAX_CARD_FILE_BYTES) {
    return `That file is ${formatBytes(file.size)}; a card sheet can be at most 1 MB (most are a few KB). Upload the corporate-card sheet.`;
  }
  return null;
}

/**
 * The "Place orders" form.
 *
 * Three things here are load-bearing and deliberately loud:
 *  - the price target, which is the user's only safety net against an
 *    unexpected final amount — so a run cannot start without one unless
 *    "No price cap" is ticked on purpose (audit F-23); a blank box is an error,
 *    never a quiet "no cap",
 *  - the card sheet, which is sent straight to the server for this run and is
 *    never stored, never echoed back, and dropped from state the moment the
 *    request has been handed off, and
 *  - which quantity mode the run is in. Spending per account and spending to a
 *    total cost completely different things, so the form always says, in words,
 *    how many orders of how many units it is about to attempt.
 */
const RUN_MODES: { value: RunMode; label: string; help: string }[] = [
  { value: "real", label: "Real", help: "Places real orders on Flipkart." },
  {
    value: "mock",
    label: "Mock",
    help: "A dry run: no browser opens and nothing is bought. Each order's outcome is made up.",
  },
  {
    value: "ai",
    label: "AI",
    help: "Paced batches: a small batch first, bigger ones at once while orders are placing, a pause when they are not.",
  },
];

const DEFAULT_AI: {
  interval: string;
  minAttempts: string;
  scaleUpMode: "percent" | "count";
  scaleUp: string;
  scaleUpCount: string;
  duration: string;
  priceTracker: boolean;
  rehearsal: boolean;
} = {
  interval: "1",
  minAttempts: "10",
  scaleUpMode: "percent",
  scaleUp: "20",
  scaleUpCount: "2",
  duration: "12h",
  priceTracker: false,
  rehearsal: false,
};
const DURATION_RE = /^\s*\d+(\.\d+)?\s*[mhd]\s*$/i;

/** Real or Mock, as one row of buttons: the choice that decides whether money is spent. */
function RunModeSwitch({
  value,
  onChange,
  disabled,
}: {
  value: RunMode;
  onChange: (mode: RunMode) => void;
  disabled: boolean;
}) {
  return (
    <div role="radiogroup" aria-label="Run mode" className="inline-flex w-fit rounded-md border border-line p-0.5">
      {RUN_MODES.map((option) => {
        const on = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={on}
            title={option.help}
            disabled={disabled}
            onClick={() => onChange(option.value)}
            className={`rounded px-3 py-1 text-[13px] font-medium ${
              on ? "bg-accent text-white" : "text-ink-2 hover:text-ink"
            } disabled:opacity-60`}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

export function OrderForm({ accounts, runActive, onStarted, className = "" }: OrderFormProps) {
  /* Real places orders; Mock rehearses the whole run without a browser. Back
     to Real after every run, like "No price cap": a dry run is chosen each time. */
  const [runMode, setRunMode] = useState<RunMode>("real");
  /* Upto takes what Flipkart allows per order; Minimum refuses a short order. */
  const [qtyMode, setQtyMode] = useState<QtyMode>("upto");
  const mock = runMode === "mock";
  const ai = runMode === "ai";
  const [aiDraft, setAiDraft] = useState(DEFAULT_AI);
  const setAi = (key: keyof typeof DEFAULT_AI, value: string | boolean) => {
    setAiDraft((prev) => ({ ...prev, [key]: value }));
    setSubmitError(null);
  };
  /** Nothing is bought: Mock, or an AI rehearsal. */
  const dryRun = mock || (ai && aiDraft.rehearsal);
  /* Refuse an order whose checkout does not show the GST it bills under. */
  const [gstMandatory, setGstMandatory] = useState(false);
  /* Delivery phone numbers used by the accounts in turn; blank keeps each
     account's own, or the address's own when that box is ticked. */
  const [phonesText, setPhonesText] = useState("");
  const [useAddressPhone, setUseAddressPhone] = useState(false);
  /* Which accounts to prefer: skip those that bought recently, and try the
     ones that have gone longest without an order first. */
  const [excludeDays, setExcludeDays] = useState("");
  const [smartSelection, setSmartSelection] = useState(false);
  /* Card pool rules, each blank for off. */
  const [cardRules, setCardRules] = useState({ cooldown: "", leastUsed: "", maxUses: "", failed: "", smart: "", minRemaining: "" });
  /* Smart cap on, and the failure codes that count against a card. */
  const [smartCap, setSmartCap] = useState(false);
  const [excludeCodesOn, setExcludeCodesOn] = useState(false);
  const [excludeCodes, setExcludeCodes] = useState<string[]>([]);
  const [failureCodes, setFailureCodes] = useState<{ code: string; name: string }[]>([]);
  /* The sheet's Card Type to use; blank uses every row. */
  const [cardType, setCardType] = useState("");
  useEffect(() => {
    let alive = true;
    listCardFailureCodes().then(
      (codes) => alive && setFailureCodes(codes),
      () => undefined,
    );
    return () => {
      alive = false;
    };
  }, []);
  const setCardRule = (key: keyof typeof cardRules, value: string) => {
    setCardRules((prev) => ({ ...prev, [key]: value }));
    setSubmitError(null);
  };
  const phones = useMemo(() => splitPhones(phonesText), [phonesText]);
  /* A product picked in the Product Screener arrives as ?product=<link>. */
  const [productLink, setProductLink] = useState(() => {
    try {
      return new URLSearchParams(window.location.search).get("product") ?? "";
    } catch {
      return "";
    }
  });
  /* Find the product by its link, or by searching Flipkart for it. */
  const [productTab, setProductTab] = useState<"url" | "search">("url");
  /* A free-text reference carried onto every order of the run. */
  const [refCode, setRefCode] = useState("");
  /* How fast the run goes: Normal works two accounts at a time, High the configured number. */
  const [executionVersion, setExecutionVersion] = useState("high_mcc");
  /* Run only on agents kept for tests, and keep them from everyone else's runs. */
  const [isolatedAgents, setIsolatedAgents] = useState(false);
  /* "+ Add product URL": more products in the same order. */
  const [extraLinks, setExtraLinks] = useState<string[]>([]);
  /* The gift card sheet of a Gift Voucher run (never stored). */
  const [giftFile, setGiftFile] = useState<File | null>(null);
  const [giftFileKey, setGiftFileKey] = useState(0);
  /* Section 4's toggles. */
  const [minutes, setMinutes] = useState(false);
  const [couponsOn, setCouponsOn] = useState(false);
  const [couponText, setCouponText] = useState("");
  const [rewardsOn, setRewardsOn] = useState(false);
  const [rewardText, setRewardText] = useState("");
  /* Balances taken off the total at checkout. */
  const [useSupercoins, setUseSupercoins] = useState(false);
  const [minSupercoins, setMinSupercoins] = useState("0");
  const [additionalOffer, setAdditionalOffer] = useState(false);
  const [useWallet, setUseWallet] = useState(false);
  const formId = useId();
  const cardTypeListId = useId();
  const [target, setTarget] = useState("");
  /* "No price cap", ticked on purpose. The only way to run without a cap: a
     blank Target is refused, here and by the server. */
  const [noCap, setNoCap] = useState(false);
  /* Total quantity, units per order, the attempt ceiling and the facts the
     unit limits are matched on. "Units per order" is the only place an order's
     size is set — it travels as both `qty` and `qty_per_order` so the two modes
     can never disagree about how big one order is. */
  const [quantity, setQuantity] = useState<QuantityDraft>(EMPTY_QUANTITY);
  const [paymentMode, setPaymentMode] = useState<PaymentMode>("card");
  /* Which address this run ships to and which GST it bills under. The picker
     fills both in from the rows marked default as soon as it has loaded. A null
     address means none is chosen yet — the run cannot start until one is; a
     null GST means automatic (the address's linked GST, else the default). */
  const [place, setPlace] = useState<AddressGstChoice>({ address_id: null, gst_id: null });
  /* The chosen address and GST in full, for the Order preview beside the form. */
  const [placeDetails, setPlaceDetails] = useState<{ address: Address | null; gst: Gst | null }>({
    address: null,
    gst: null,
  });
  const onPlaceResolved = useCallback(
    (address: Address | null, gst: Gst | null) => setPlaceDetails({ address, gst }),
    [],
  );
  /* The seller every order in the run buys from. Blank is Flipkart's default
     seller, exactly as before this field existed; the list offers the sellers
     the preview read, and any other name can be typed. */
  const [seller, setSeller] = useState("");
  const sellerListId = useId();
  const [cardsFile, setCardsFile] = useState<File | null>(null);
  /* A person finishes the bank's step in the window the run shows. Ticked for
     one run at a time, like "No price cap". */
  const [bankByHand, setBankByHand] = useState(false);
  const [fileKey, setFileKey] = useState(0);
  const [fileNote, setFileNote] = useState<string | null>(null);

  /* Starts as "every account with a valid session": a filter, so the run can
     be aimed at thousands of accounts without the browser listing them. The
     picker corrects the count as soon as the server answers. */
  const [selection, setSelection] = useState<AccountSelection>({
    mode: "filter",
    filter: { statuses: ["session_valid"] },
    count: 0,
  });

  /* Section 2's source: every matching email id, or the ones in an uploaded file. */
  const [emailSource, setEmailSource] = useState<"all" | "csv">("all");
  const [emailFile, setEmailFile] = useState<{ name: string; matched: number; unmatched: string[]; notEligible: string[] } | null>(null);
  const [emailFileError, setEmailFileError] = useState<string | null>(null);
  const [emailFileKey, setEmailFileKey] = useState(0);
  const useAllEmails = () => {
    setEmailSource("all");
    setEmailFile(null);
    setEmailFileError(null);
    setEmailFileKey((key) => key + 1);
    setSelection({ mode: "filter", filter: { statuses: ["session_valid"] }, count: 0 });
  };
  const onEmailFile = async (file: File | null) => {
    setEmailFileError(null);
    if (file === null) return;
    const emails = emailsInText(await file.text());
    if (emails.length === 0) {
      setEmailFile(null);
      setSelection({ mode: "ids", ids: [] });
      setEmailFileError("No email address found in that file.");
      return;
    }
    try {
      const found = await accountsByEmails({ emails, statuses: ["session_valid"] });
      setEmailFile({ name: file.name, matched: found.ids.length, unmatched: found.unmatched, notEligible: found.not_eligible });
      setSelection({ mode: "ids", ids: found.ids });
    } catch (err) {
      setEmailFileError(errorMessage(err));
    }
  };

  const [showErrors, setShowErrors] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  /* --- the product preview -------------------------------------------------
     What the pasted link actually points at, read before the run commits to
     it. It is more than decoration: it is also what fills Brand and Category,
     which are the facts the per-brand and per-category unit limits match on.
  ---------------------------------------------------------------------- */
  const [preview, setPreview] = useState<ProductPreviewData | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  /* Every fetch takes the next id, and a reply carrying anything but the
     current id is dropped — so typing fast can never land an older product's
     details on top of a newer one. */
  const previewRequestId = useRef(0);
  /* Which of the two facts the user has typed into themselves. Those are the
     ones the preview never touches again. */
  const editedFacts = useRef({ brand: false, category: false });

  const locked = runActive || submitting;

  /** How many accounts the selection holds. */
  const runCount = selectionCount(selection);
  /* Of a selection by id the page has loaded, how many have a GST of their
     own: the summary says those bill under it, not the run's. Unknown for a
     selection by filter, or one reaching past the loaded accounts. */
  const ownGst = useMemo(() => {
    if (selection.mode !== "ids" || selection.ids.length === 0) return null;
    const byId = new Map(accounts.accounts.map((account) => [account.id, account]));
    const chosen = selection.ids.map((id) => byId.get(id));
    if (chosen.some((account) => account === undefined)) return null;
    return { count: chosen.filter((account) => account?.gst != null).length, of: chosen.length };
  }, [selection, accounts.accounts]);
  /** No address chosen: the server would refuse the run, so say so up front. */
  const noAddress = place.address_id === null;
  /** Only meaningful once the first page has landed. */
  const noneEligible = !accounts.loading && accounts.total > 0 && runCount === 0;

  /* The attempt ceiling is a ceiling on ACCOUNTS, so it — not the selection —
     is what the plan below multiplies by. */
  const attemptCeiling = numberOrNull(quantity.totalAttempts);
  const accountsTried =
    attemptCeiling === null
      ? runCount
      : Math.max(0, Math.min(Math.trunc(attemptCeiling), runCount));

  const quantityErrors = useMemo(() => validateQuantity(quantity), [quantity]);
  const plan = useMemo(() => orderPlan(quantity, accountsTried), [quantity, accountsTried]);

  const errors = useMemo((): FormErrors => {
    const next: FormErrors = {};

    if (productLink.trim() === "") {
      next.product_link = "Paste the Flipkart product link.";
    } else if (!urlIsUsable(productLink)) {
      next.product_link = "That does not look like a full link. Start it with https://";
    }

    // Ticked, the box says on purpose that there is no cap, and the Target is
    // cleared and turned off. Unticked, a cap is required.
    if (!noCap) {
      if (target.trim() === "") {
        next.target = "Enter a price cap, or tick “No price cap” to order at any price.";
      } else {
        const targetNumber = Number(target);
        if (!Number.isFinite(targetNumber)) {
          next.target = "Enter the cap as a number in rupees, or tick “No price cap”.";
        } else if (targetNumber <= 0) {
          next.target = "A target of zero or less would fail every order. Enter a price above zero.";
        }
      }
    }

    // Cash on Delivery involves no card, and Mock rehearses without one.
    if (usesCard(paymentMode) && runMode !== "mock") {
      if (cardsFile === null) {
        next.cards_file = "Upload the corporate-card sheet (.xlsx or .csv) for this run.";
      } else {
        const problem = cardSheetProblem(cardsFile);
        if (problem !== null) next.cards_file = problem;
      }
    }

    // The box holds no more than this, but a value can still arrive longer
    // (pasted by a script, say); the server would refuse it, so say so here.
    if (seller.trim().length > MAX_SELLER_CHARS) {
      next.seller = `A seller's name is at most ${MAX_SELLER_CHARS} characters.`;
    }

    const badPhones = phones.filter((phone) => !/^\d{10}$/.test(phone));
    if (badPhones.length > 0) {
      next.address_phones = `Each number must be 10 digits without the country code: ${badPhones.slice(0, 5).join(", ")}.`;
    }

    if (runMode === "ai") {
      const interval = Number(aiDraft.interval);
      const minAttempts = Number(aiDraft.minAttempts);
      const scaleUp = Number(aiDraft.scaleUp);
      const scaleUpCount = Number(aiDraft.scaleUpCount);
      if (!(interval >= 1 && interval <= 60)) next.ai = "Max interval is 1 to 60 minutes.";
      else if (!Number.isInteger(minAttempts) || minAttempts < 1 || minAttempts > 30)
        next.ai = "Minimum attempts per interval is a whole number from 1 to 30.";
      else if (aiDraft.scaleUpMode === "count" && (!Number.isInteger(scaleUpCount) || scaleUpCount < 1 || scaleUpCount > 30))
        next.ai = "Scale-up fixed count is a whole number from 1 to 30 orders.";
      else if (aiDraft.scaleUpMode !== "count" && (!Number.isInteger(scaleUp) || scaleUp < 1 || scaleUp > 90))
        next.ai = "Scale-up threshold is a whole number from 1 to 90 %.";
      else if (!DURATION_RE.test(aiDraft.duration))
        next.ai = "Write the run duration like 12h, 90m or 2d (between 1h and 2d).";
    }

    if (usesGiftCards(paymentMode) && runMode !== "mock" && giftFile === null) {
      next.gift_cards = "Upload the gift card sheet (Email, Voucher Number, Voucher PIN) for this run.";
    }

    const urlsCount = 1 + extraLinks.filter((link) => link.trim()).length;
    if (urlsCount > 1) {
      const perOrder = Number(quantity.qtyPerOrder);
      if (Number.isInteger(perOrder) && perOrder % urlsCount !== 0) {
        next.extra_links = "Quantity per order must be divisible by the number of product URLs.";
      } else if (extraLinks.some((link) => link.trim() && !urlIsUsable(link))) {
        next.extra_links = "Every additional product URL must be a full link starting with https://";
      }
    }

    if (usesCard(paymentMode)) {
      const limits: [string, number][] = [
        [cardRules.cooldown, 365],
        [cardRules.leastUsed, 365],
        [cardRules.maxUses, 1000],
        [cardRules.failed, 365],
        [smartCap ? cardRules.smart : "", 1000],
      ];
      for (const [text, max] of limits) {
        if (text.trim() === "") continue;
        const value = Number(text);
        if (!Number.isInteger(value) || value < 1 || value > max) {
          next.card_rules = "Each card filter is a whole number from 1 up (days up to 365, uses up to 1000), or blank for off.";
          break;
        }
      }
      if (cardRules.minRemaining.trim() !== "") {
        const rupees = Number(cardRules.minRemaining);
        if (!Number.isFinite(rupees) || rupees < 1 || rupees > 10_000_000) {
          next.card_rules = "Minimum remaining is an amount in rupees from 1 up, or blank for off.";
        }
      }
      if (smartCap && cardRules.smart.trim() === "") {
        next.card_rules = "Enter how many orders a card may pay for in 30 days, or turn Smart Max Card Usage off.";
      }
      if (excludeCodesOn && excludeCodes.length === 0) {
        next.card_rules = "Pick at least one failure error, or turn the exclusion off.";
      }
    }

    if (excludeDays.trim() !== "") {
      const days = Number(excludeDays);
      if (!Number.isInteger(days) || days < 1 || days > 365) {
        next.exclude_days = "Enter a whole number of days from 1 to 365, or leave it blank.";
      }
    }

    return next;
  }, [productLink, target, noCap, cardsFile, paymentMode, seller, phones, excludeDays, cardRules, runMode, aiDraft, smartCap, excludeCodesOn, excludeCodes, giftFile, extraLinks, quantity.qtyPerOrder]);

  const shown = showErrors ? errors : {};
  const shownQuantity = showErrors ? quantityErrors : {};
  /* A picked file that cannot be sent is said at once, not only after Start:
     no pairing preview appears for it, and nothing else would say why. */
  const cardsFileError = showErrors || cardsFile !== null ? errors.cards_file : undefined;
  /** The picked sheet, when it may be sent (its pairing previewed, Start sending it). */
  const sendableSheet = cardsFile !== null && cardSheetProblem(cardsFile) === null ? cardsFile : null;

  /** The link to preview — "" until it is a usable http(s) URL. */
  const previewUrl = urlIsUsable(productLink) ? productLink.trim() : "";
  /** Only `false` is a warning. `null` means the page did not say. */
  const outOfStock = preview?.in_stock === false;

  /* The sellers the preview read, and what the typed name is among them.
     Neither note below blocks the run: the run itself checks the seller on
     Flipkart's own page, and stops before anything is pressed if it is not
     listed there — the preview may be minutes old. */
  const offeredSellers = preview?.sellers ?? [];
  const defaultSeller = preview?.default_seller ?? preview?.seller ?? null;
  const chosenSeller = seller.trim();
  const chosenOffer = offeredSellers.find((offer) => sameSeller(offer.name, chosenSeller));
  const sellerNotListed = chosenSeller !== "" && offeredSellers.length > 0 && chosenOffer === undefined;
  const sellerCartOnly = chosenOffer?.buy_now === false;

  /**
   * Fill Brand and Category from the preview, but only where the user has not
   * typed their own value: these two feed the per-brand and per-category unit
   * limits, so filling them in is the whole point — and clobbering a deliberate
   * entry would be worse than not prefilling at all.
   *
   * A value this put there earlier is still ours to correct, so switching to a
   * different product updates it rather than leaving the old brand behind.
   */
  const prefillFacts = (result: ProductPreviewData) => {
    const brand = result.brand?.trim() ?? "";
    const category = result.category?.trim() ?? "";
    setQuantity((prev) => {
      const next = { ...prev };
      let changed = false;
      if (brand !== "" && !editedFacts.current.brand && prev.brand !== brand) {
        next.brand = brand;
        changed = true;
      }
      if (category !== "" && !editedFacts.current.category && prev.category !== category) {
        next.category = category;
        changed = true;
      }
      return changed ? next : prev;
    });
  };

  useEffect(() => {
    // Taking the next id first retires whatever is already in the air: its
    // reply will no longer match and is dropped on arrival.
    previewRequestId.current += 1;
    const id = previewRequestId.current;

    if (previewUrl === "") {
      setPreview(null);
      setPreviewError(null);
      setPreviewLoading(false);
      return;
    }

    // The previous product's card must never sit under a link that no longer
    // points at it, so the card drops straight into its loading state.
    setPreview(null);
    setPreviewError(null);
    setPreviewLoading(true);

    const timer = window.setTimeout(() => {
      previewProduct(previewUrl).then(
        (result) => {
          if (previewRequestId.current !== id) return;
          setPreviewLoading(false);
          setPreview(result);
          prefillFacts(result);
        },
        (err: unknown) => {
          if (previewRequestId.current !== id) return;
          setPreviewLoading(false);
          setPreviewError(errorMessage(err));
        },
      );
    }, PREVIEW_DEBOUNCE_MS);

    // A newer link (or an unmount) cancels the pending fetch outright; one
    // already sent is handled by the id check above.
    return () => window.clearTimeout(timer);
    // `previewUrl` is the only value the request is built from, so the closure
    // cannot read a stale link; the prefill reads nothing but the reply.
  }, [previewUrl]);

  /* "Run again" on the Orders page opens this form as /ordering?from=<run>:
     every setting the run recorded is filled in, and nothing starts until
     Submit. Card sheets are never stored, so a card run asks for its sheet again. */
  const [searchParams] = useSearchParams();
  const fromRun = searchParams.get("from");
  const [prefillNote, setPrefillNote] = useState<string | null>(null);
  useEffect(() => {
    const id = Number(fromRun);
    if (!fromRun || !Number.isInteger(id) || id < 1) return;
    let alive = true;
    getJob(id).then(
      (job) => {
        if (!alive) return;
        const p = (job.payload ?? {}) as Record<string, unknown>;
        const text = (key: string) => (p[key] === undefined || p[key] === null ? "" : String(p[key]));
        if (p.product_link) setProductLink(String(p.product_link));
        if (p.no_cap) {
          setNoCap(true);
          setTarget("");
        } else if (p.target !== undefined && p.target !== null) setTarget(String(p.target));
        if (p.mode === "mock" || p.mode === "ai") setRunMode(p.mode);
        if (p.payment_mode === "cod" || p.payment_mode === "card") setPaymentMode(p.payment_mode);
        if (p.qty_mode === "minimum") setQtyMode("minimum");
        setQuantity((prev) => ({
          ...prev,
          totalQuantity: text("total_quantity"),
          qtyPerOrder: text("qty_per_order") || text("qty") || prev.qtyPerOrder,
          totalAttempts: text("total_attempts"),
          cartAmountLimit: text("cart_amount_limit"),
          maxDeliveryDays: text("max_delivery_days") || "3",
          brand: text("brand") || prev.brand,
          category: text("category") || prev.category,
        }));
        if (p.brand) editedFacts.current.brand = true;
        if (p.category) editedFacts.current.category = true;
        if (p.seller) setSeller(String(p.seller));
        if (p.ref_code) setRefCode(String(p.ref_code));
        if (p.execution_version) setExecutionVersion(String(p.execution_version).replace(/_mcc$/, ""));
        if (p.isolated_agents) setIsolatedAgents(true);
        if (p.use_supercoins) {
          setUseSupercoins(true);
          setMinSupercoins(text("min_supercoins") || "0");
        }
        if (p.use_wallet) setUseWallet(true);
        if (Array.isArray(p.extra_product_links)) setExtraLinks(p.extra_product_links.map(String));
        if (p.flipkart_minutes) setMinutes(true);
        if (Array.isArray(p.coupon_codes) && p.coupon_codes.length) {
          setCouponsOn(true);
          setCouponText(p.coupon_codes.join(", "));
        }
        if (Array.isArray(p.reward_ids) && p.reward_ids.length) {
          setRewardsOn(true);
          setRewardText(p.reward_ids.join(", "));
        }
        if (p.apply_additional_offer) setAdditionalOffer(true);
        if (p.gst_mandatory) setGstMandatory(true);
        if (p.use_address_phone) setUseAddressPhone(true);
        if (p.exclude_ordered_days) setExcludeDays(String(p.exclude_ordered_days));
        if (p.smart_selection) setSmartSelection(true);
        if (p.card_type) setCardType(String(p.card_type));
        if (p.address_id || p.gst_id) {
          setPlace({
            address_id: typeof p.address_id === "number" ? p.address_id : null,
            gst_id: typeof p.gst_id === "number" ? p.gst_id : null,
          });
        }
        setCardRules((prev) => ({
          ...prev,
          cooldown: text("card_cooldown_days"),
          leastUsed: text("card_least_used_days"),
          maxUses: text("card_max_uses"),
          failed: text("card_exclude_failed_days"),
          smart: text("card_smart_max_uses"),
          minRemaining: text("card_min_remaining"),
        }));
        if (p.card_smart_max_uses) setSmartCap(true);
        if (Array.isArray(p.card_exclude_failed_codes) && p.card_exclude_failed_codes.length) {
          setExcludeCodesOn(true);
          setExcludeCodes(p.card_exclude_failed_codes.map(String));
        }
        setPrefillNote(
          `Filled in from run #${job.id}.${p.payment_mode === "card" && p.mode !== "mock" ? " Upload the card sheet again: card details are never kept." : ""} Check it, then Submit.`,
        );
      },
      () => alive && setPrefillNote(`Run #${fromRun} could not be read; the form is left as it was.`),
    );
    return () => {
      alive = false;
    };
    // Once per "from": the setters are stable.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fromRun]);

  const onFileChange = (file: File | null) => {
    setCardsFile(file);
    setFileNote(null);
    setSubmitError(null);
  };

  const setQuantityField = (key: keyof QuantityDraft, value: string) => {
    // A fact the user has typed in is theirs from then on — the preview stops
    // filling it, even if they clear it again.
    if (key === "brand" || key === "category") editedFacts.current[key] = true;
    setQuantity((prev) => ({ ...prev, [key]: value }));
    setSubmitError(null);
  };

  /** Every field back to how the page opened (the run's accounts are kept). */
  const resetToDefaults = () => {
    setRunMode("real");
    setAiDraft(DEFAULT_AI);
    setQtyMode("upto");
    setGstMandatory(false);
    setPhonesText("");
    setUseAddressPhone(false);
    setExcludeDays("");
    setSmartSelection(false);
    setCardRules({ cooldown: "", leastUsed: "", maxUses: "", failed: "", smart: "", minRemaining: "" });
    setSmartCap(false);
    setExcludeCodesOn(false);
    setExcludeCodes([]);
    setCardType("");
    setProductLink("");
    setProductTab("url");
    setRefCode("");
    setExtraLinks([]);
    setGiftFile(null);
    setGiftFileKey((key) => key + 1);
    setMinutes(false);
    setCouponsOn(false);
    setCouponText("");
    setRewardsOn(false);
    setRewardText("");
    setExecutionVersion("high_mcc");
    setIsolatedAgents(false);
    setUseSupercoins(false);
    setMinSupercoins("0");
    setAdditionalOffer(false);
    setUseWallet(false);
    setTarget("");
    setNoCap(false);
    setQuantity(EMPTY_QUANTITY);
    editedFacts.current = { brand: false, category: false };
    setPaymentMode("card");
    setSeller("");
    setCardsFile(null);
    setFileKey((key) => key + 1);
    setFileNote(null);
    setBankByHand(false);
    setShowErrors(false);
    setSubmitError(null);
  };

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (locked) return;

    setShowErrors(true);
    setSubmitError(null);
    if (Object.keys(errors).length > 0) return;
    if (Object.keys(quantityErrors).length > 0) return;
    if (usesCard(paymentMode) && !mock && cardsFile === null) return;
    if (noAddress) return;

    setSubmitting(true);
    try {
      const job = await startOrderJob({
        mode: runMode,
        qty_mode: qtyMode,
        ai: ai
          ? {
              max_interval_minutes: Number(aiDraft.interval),
              min_attempts: Number(aiDraft.minAttempts),
              scale_up_pct: Number(aiDraft.scaleUp),
              ...(aiDraft.scaleUpMode === "count"
                ? { scale_up_mode: "count" as const, scale_up_count: Number(aiDraft.scaleUpCount) }
                : {}),
              run_duration: aiDraft.duration.trim(),
              ...(aiDraft.priceTracker ? { single_probe: true, recycle_accounts: true } : {}),
              rehearsal: aiDraft.rehearsal,
            }
          : undefined,
        product_link: productLink.trim(),
        // One control sets an order's size, sent under both names: `qty` is
        // what the single-order mode places, `qty_per_order` is the batch the
        // total is bought in. Two controls here would be two sources of truth.
        qty: plan.unitsPerOrder,
        // Exactly one of the two, as the server requires: a cap, or "no cap"
        // said on purpose.
        target: noCap ? null : Number(target),
        no_cap: noCap,
        // One of the two, never both: a filter when the run is aimed at
        // "everything matching", explicit ids when rows were ticked.
        account_ids: selection.mode === "ids" ? selection.ids : null,
        account_filter: selection.mode === "filter" ? selection.filter : undefined,
        payment_mode: paymentMode,
        cards_file: usesCard(paymentMode) && !mock ? cardsFile : null,
        gift_cards_file: usesGiftCards(paymentMode) && !mock ? giftFile : null,
        extra_product_links: extraLinks.map((link) => link.trim()).filter(Boolean),
        coupon_codes: couponsOn ? splitList(couponText) : [],
        reward_ids: SHOW_UNMAPPED_OPTIONS && rewardsOn ? splitList(rewardText) : [],
        flipkart_minutes: SHOW_UNMAPPED_OPTIONS && minutes,
        ref_code: refCode.trim() || null,
        execution_version: executionVersion,
        isolated_agents: isolatedAgents,
        use_supercoins: useSupercoins,
        min_supercoins: useSupercoins ? numberOrNull(minSupercoins) : null,
        use_wallet: useWallet,
        apply_additional_offer: SHOW_UNMAPPED_OPTIONS && additionalOffer,
        address_id: place.address_id,
        gst_id: place.gst_id,
        // null for an unset field, which the client omits from the request —
        // and an omitted total_quantity is what keeps the old behaviour.
        total_quantity: plan.totalUnits,
        qty_per_order: plan.unitsPerOrder,
        total_attempts: numberOrNull(quantity.totalAttempts),
        cart_amount_limit: numberOrNull(quantity.cartAmountLimit),
        max_delivery_days: numberOrNull(quantity.maxDeliveryDays),
        brand: quantity.brand.trim() || null,
        category: quantity.category.trim() || null,
        // The product's title, so a model limit ("T5x 5G") can match it.
        model: preview?.name?.trim() || null,
        // Blank is Flipkart's default seller, and is left out of the request.
        seller: chosenSeller || null,
        // A mock run opens no bank page, so there is nothing to finish by hand.
        bank_step_by_hand: !dryRun && usesCard(paymentMode) && bankByHand,
        gst_mandatory: gstMandatory,
        address_phones: phones,
        use_address_phone: phones.length === 0 && useAddressPhone,
        exclude_ordered_days: numberOrNull(excludeDays),
        smart_selection: smartSelection,
        card_cooldown_days: mock ? null : numberOrNull(cardRules.cooldown),
        card_least_used_days: numberOrNull(cardRules.leastUsed),
        card_max_uses: numberOrNull(cardRules.maxUses),
        card_exclude_failed_days: numberOrNull(cardRules.failed),
        card_smart_max_uses: smartCap ? numberOrNull(cardRules.smart) : null,
        card_min_remaining: numberOrNull(cardRules.minRemaining),
        card_exclude_failed_codes: excludeCodesOn ? excludeCodes : [],
        card_type: cardType.trim() || null,
      });
      // The sheet has been handed to the request. Let go of it immediately:
      // nothing on this client should hold card data a moment longer.
      setCardsFile(null);
      setFileKey((key) => key + 1);
      setFileNote(
        `Sent with run #${job.id} and cleared from this form. Upload the sheet again for another run.`,
      );
      // Every run states its own cap: an uncapped run is ticked for that run
      // only, never carried over to the next one started from this page.
      setNoCap(false);
      // Likewise a person at the screen for the bank's step: that run only.
      setBankByHand(false);
      // And a dry run: the next run is real unless Mock is picked again.
      setRunMode("real");
      setShowErrors(false);
      onStarted(job);
    } catch (err) {
      setSubmitError(errorMessage(err));
    } finally {
      setSubmitting(false);
    }
  };

  const submitLabel = submitting
    ? "Submitting…"
    : mock
      ? "Submit Mock Order"
      : ai
        ? aiDraft.rehearsal
          ? "Submit AI Rehearsal"
          : "Submit AI Order"
        : "Submit Order";

  const statusLine = runActive
    ? "A run is in progress. Wait for it to finish, or stop it below."
    : noAddress
      ? "Choose a delivery address — a run cannot start without one."
      : runCount === 0
        ? "No account selected — nothing to order with yet."
        : `${
            plan.mode === "total"
              ? `${describePlan(plan)}, across up to ${countLabel(accountsTried, "account")}. `
              : `${countLabel(accountsTried, "order")} of ${countLabel(plan.unitsPerOrder, "unit")} will be attempted — one per account. `
          }${
            noCap
              ? "No cap set — orders will be placed at any amount."
              : target.trim() === ""
                ? "Enter a price cap above, or tick “No price cap”."
                : "Orders over the target will not be placed."
          }${
            attemptCeiling !== null && attemptCeiling < runCount
              ? ` The attempt ceiling stops this at ${formatCount(accountsTried)} of the ${formatCount(runCount)} selected accounts.`
              : ""
          }`;

  return (
    <FieldLookProvider value="boxed">
      <div className={`flex flex-col gap-4 ${className}`}>
        <div className="flex flex-wrap items-center justify-end gap-3">
          {prefillNote ? <p className="mr-auto text-[13px] text-ink-2">{prefillNote}</p> : null}
          <Button type="button" variant="secondary" size="sm" disabled={locked} onClick={resetToDefaults}>
            Reset to defaults
          </Button>
        </div>
        <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_340px]">
          <form
            id={formId}
            aria-label="Smart bulk order"
            className="flex min-w-0 flex-col gap-4"
            onSubmit={(event) => void onSubmit(event)}
            noValidate
          >
            {/* --- 1. Product ------------------------------------------------ */}
            <FormSection
              title="1. Product"
              lead={
                <RunModeSwitch
                  value={runMode}
                  disabled={locked}
                  onChange={(next) => {
                    setRunMode(next);
                    setSubmitError(null);
                  }}
                />
              }
            >
              {mock ? (
                <Alert tone="info" title="Mock run: nothing is bought">
                  Accounts, sessions, cards and unit limits are checked as for a real run, then each
                  order&rsquo;s outcome is made up. No browser opens, nothing is charged and no order is
                  added to the ledger.
                </Alert>
              ) : null}

              <Field label="Platform" required>
                <Select name="platform" value="flipkart" disabled={locked} onChange={() => undefined}>
                  <option value="flipkart">FLIPKART</option>
                </Select>
              </Field>

              <Field
                label="Order execution version"
                help="Normal Speed works through two accounts at a time, which is gentler on the platform. High Speed works through as many as Settings allows."
              >
                <Select
                  name="execution_version"
                  value={executionVersion}
                  disabled={locked}
                  onChange={(event) => setExecutionVersion(event.target.value)}
                >
                  {EXECUTION_VERSIONS.map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </Select>
              </Field>

              <Checkbox
                name="isolated_agents"
                label={
                  <span className="flex flex-col">
                    <span>Test on isolated agents</span>
                    <span className="text-[12px] text-ink-2">
                      Assigns this bulk only to isolated agents. Other users&rsquo; work will not use those agents.
                    </span>
                  </span>
                }
                checked={isolatedAgents}
                disabled={locked}
                onChange={(event) => setIsolatedAgents(event.target.checked)}
              />

              <div role="tablist" aria-label="Find the product by" className="flex gap-2">
                {(
                  [
                    ["url", "URL"],
                    ["search", "Search"],
                  ] as const
                ).map(([value, label]) => (
                  <button
                    key={value}
                    type="button"
                    role="tab"
                    aria-selected={productTab === value}
                    disabled={locked}
                    onClick={() => setProductTab(value)}
                    className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-[12px] font-medium transition-colors ${
                      productTab === value
                        ? "border-accent bg-accent/10 text-accent"
                        : "border-line text-ink-2 hover:text-ink"
                    }`}
                  >
                    {value === "url" ? (
                      <svg viewBox="0 0 16 16" className="size-3.5" aria-hidden="true">
                        <path
                          d="M6.5 9.5l3-3M7 4.5l1-1a2.5 2.5 0 0 1 3.5 3.5l-1 1M9 11.5l-1 1A2.5 2.5 0 0 1 4.5 9l1-1"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="1.5"
                          strokeLinecap="round"
                        />
                      </svg>
                    ) : (
                      <svg viewBox="0 0 16 16" className="size-3.5" aria-hidden="true">
                        <circle cx="7" cy="7" r="4" fill="none" stroke="currentColor" strokeWidth="1.5" />
                        <path d="M10 10l3.5 3.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                      </svg>
                    )}
                    {label}
                  </button>
                ))}
              </div>

              {productTab === "search" ? (
                <FieldLookProvider value="stacked">
                  {/* Picking a result fills the link and flips back to URL. */}
                  <ProductSearch
                    onPick={(link) => {
                      setProductLink(link);
                      setProductTab("url");
                    }}
                    disabled={locked}
                  />
                </FieldLookProvider>
              ) : (
                <Field
                  label="Product URL"
                  required
                  error={shown.product_link}
                  help="The Flipkart product page to order from. Every account in the run orders this item."
                >
                  <TextInput
                    name="product_link"
                    type="url"
                    inputMode="url"
                    autoComplete="off"
                    spellCheck={false}
                    placeholder="https://www.flipkart.com/…"
                    value={productLink}
                    disabled={locked}
                    onChange={(event) => setProductLink(event.target.value)}
                  />
                </Field>
              )}

              {productTab === "url" ? (
                <div className="flex flex-col gap-2">
                  {extraLinks.map((link, index) => (
                    <div key={index} className="flex items-start gap-2">
                      <Field label={`Additional product URL ${index + 2}`} className="flex-1">
                        <TextInput
                          name={`extra_product_link_${index + 2}`}
                          type="url"
                          autoComplete="off"
                          spellCheck={false}
                          value={link}
                          disabled={locked}
                          onChange={(event) =>
                            setExtraLinks((current) => current.map((old, at) => (at === index ? event.target.value : old)))
                          }
                        />
                      </Field>
                      <Button
                        type="button"
                        size="sm"
                        variant="ghost"
                        className="mt-2"
                        aria-label={`Remove product URL ${index + 2}`}
                        disabled={locked}
                        onClick={() => setExtraLinks((current) => current.filter((_, at) => at !== index))}
                      >
                        ✕
                      </Button>
                    </div>
                  ))}
                  <div>
                    <Button
                      type="button"
                      size="sm"
                      variant="secondary"
                      disabled={locked || extraLinks.length >= 9}
                      onClick={() => setExtraLinks((current) => [...current, ""])}
                    >
                      + Add product URL
                    </Button>
                  </div>
                  {extraLinks.length ? (
                    <p className="text-[12px] text-ink-2">Quantity per order must be divisible by the number of product URLs.</p>
                  ) : null}
                  {shown.extra_links ?? (extraLinks.length ? errors.extra_links : undefined) ? (
                    <p role="alert" className="text-[12px] text-ink">{shown.extra_links ?? errors.extra_links}</p>
                  ) : null}
                </div>
              ) : null}

              <Field
                label="Ref Code"
                hint="Optional. Kept on every order of this run and in the exports."
              >
                <TextInput
                  name="ref_code"
                  autoComplete="off"
                  maxLength={64}
                  value={refCode}
                  disabled={locked}
                  onChange={(event) => setRefCode(event.target.value)}
                />
              </Field>

              {/* What the link points at, with who sells it. */}
              <ProductPreview preview={preview} loading={previewLoading} error={previewError} />

              <QuantityFields
                part="quantity"
                draft={quantity}
                errors={shownQuantity}
                disabled={locked}
                accountCount={accountsTried}
                onChange={setQuantityField}
                qtyMode={qtyMode}
                hideAttempts={ai}
                onQtyModeChange={(next) => {
                  setQtyMode(next);
                  setSubmitError(null);
                }}
              />

              {ai ? (
                <fieldset className="flex flex-col gap-3 rounded-control border border-line bg-page px-3.5 py-3" disabled={locked}>
                  <legend className="px-1 text-[13px] font-semibold text-ink">AI pacing</legend>
                  <div className="grid gap-3 sm:grid-cols-2">
                    <Field label="Max Interval (minutes)" hint="Max wait before the next batch" help="The longest wait before the next batch when orders are not placing. 1 to 60.">
                      <NumberInput name="ai_max_interval_minutes" min={1} max={60} step={1} value={aiDraft.interval} onChange={(e) => setAi("interval", e.target.value)} />
                    </Field>
                    <Field label="Minimum attempts per interval" hint="Range 1 to 30" help="Baseline / reset batch size when orders are not scaling up. 1 to 30.">
                      <NumberInput name="ai_min_attempts" min={1} max={30} step={1} value={aiDraft.minAttempts} onChange={(e) => setAi("minAttempts", e.target.value)} />
                    </Field>
                    <Field label="Scale-Up Threshold Mode" hint={aiDraft.scaleUpMode === "count" ? "Fixed placed count" : "Of the last batch"}>
                      <Select
                        name="ai_scale_up_mode"
                        value={aiDraft.scaleUpMode}
                        onChange={(e) => setAi("scaleUpMode", e.target.value as "percent" | "count")}
                      >
                        <option value="percent">Percentage of last batch</option>
                        <option value="count">Fixed count</option>
                      </Select>
                    </Field>
                    {aiDraft.scaleUpMode === "count" ? (
                      <Field label="Scale-Up Threshold (orders)" hint="Range 1 to 30" help="When at least this many orders place in a batch, the next batch starts at once and doubles. 1 to 30.">
                        <NumberInput name="ai_scale_up_count" min={1} max={30} step={1} value={aiDraft.scaleUpCount} onChange={(e) => setAi("scaleUpCount", e.target.value)} />
                      </Field>
                    ) : (
                      <Field label="Scale-Up Threshold (%)" hint="Range 1 to 90" help="When this share of a batch places (ceil), the next batch starts at once and doubles. 1 to 90.">
                        <NumberInput name="ai_scale_up_pct" min={1} max={90} step={1} value={aiDraft.scaleUp} onChange={(e) => setAi("scaleUp", e.target.value)} />
                      </Field>
                    )}
                    <Field label="Run duration" hint="1h to 2d" help="The run tracks and paces for this long: 12h, 90m, 2d (1h to 2d).">
                      <TextInput name="ai_run_duration" value={aiDraft.duration} onChange={(e) => setAi("duration", e.target.value)} />
                    </Field>
                    <div className="flex flex-col gap-2 self-start pt-1">
                      <Checkbox
                        name="ai_price_tracker"
                        label="Price tracker mode (1-probe watch & recycle)"
                        help="Probes 1 account per interval while price is above Final Amount Limit or out of stock, recycles non-buying accounts for the full Run Duration, and bursts to buy immediately when price drops."
                        checked={aiDraft.priceTracker}
                        onChange={(e) => setAi("priceTracker", e.target.checked)}
                      />
                      <Checkbox
                        name="ai_rehearsal"
                        label="Rehearsal: pace for real, buy nothing"
                        help="Each order's outcome is made up, as in Mock, so the batches can be watched safely."
                        checked={aiDraft.rehearsal}
                        onChange={(e) => setAi("rehearsal", e.target.checked)}
                      />
                    </div>
                  </div>
                  {shown.ai ?? errors.ai ? <p className="text-[12px] text-critical">{shown.ai ?? errors.ai}</p> : null}
                </fieldset>
              ) : null}

              {/* A seller that cancels "as per seller policy" is avoided by buying from another. */}
              <Field
                label="Seller"
                error={shown.seller}
                hint={defaultSeller ? `Default: ${defaultSeller}` : "Flipkart's default when blank"}
                help="Leave blank to buy from whoever Flipkart shows by default. Pick a seller from the list or type one: every order then buys from that seller's row on the product's sellers page, and stops before anything is pressed if the page does not list it."
              >
                <TextInput
                  name="seller"
                  list={sellerListId}
                  autoComplete="off"
                  spellCheck={false}
                  maxLength={MAX_SELLER_CHARS}
                  placeholder={defaultSeller ? `${defaultSeller} (Default)` : "Flipkart's default"}
                  value={seller}
                  disabled={locked}
                  onChange={(event) => {
                    setSeller(event.target.value);
                    setSubmitError(null);
                  }}
                />
              </Field>
              <datalist id={sellerListId}>
                {offeredSellers.map((offer) => (
                  <option key={offer.name} value={offer.name} label={sellerSummary(offer)} />
                ))}
              </datalist>
              {sellerNotListed ? (
                <p className="-mt-1 text-[12px] text-ink">
                  &ldquo;{chosenSeller}&rdquo; is not among the sellers listed for this product, so each order
                  will most likely stop. Check the spelling against the list.
                </p>
              ) : null}
              {sellerCartOnly ? (
                <p className="-mt-1 text-[12px] text-ink">
                  {chosenOffer?.name} offers only Add to cart. Choosing a seller works only for products with
                  Buy now for now, so each order will stop before anything is pressed.
                </p>
              ) : null}

              {outOfStock ? (
                <Alert tone="warning" title="This product is out of stock">
                  The product page says it is unavailable, so a run started now will most likely fail for
                  every account it tries. Start it anyway if you know the listing is wrong.
                </Alert>
              ) : null}
            </FormSection>

            {/* --- 2. Email IDs ---------------------------------------------- */}
            <FormSection title="2. Email IDs">
              <div role="tablist" aria-label="Which email ids" className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  role="tab"
                  aria-selected={emailSource === "all"}
                  disabled={locked}
                  onClick={useAllEmails}
                  className={`rounded-full border px-3 py-1 text-[12px] font-medium ${
                    emailSource === "all" ? "border-accent bg-accent/10 text-accent" : "border-line text-ink-2 hover:text-ink"
                  }`}
                >
                  All Email IDs
                </button>
                <button
                  type="button"
                  role="tab"
                  aria-selected={emailSource === "csv"}
                  disabled={locked}
                  onClick={() => {
                    setEmailSource("csv");
                    setSelection({ mode: "ids", ids: [] });
                  }}
                  className={`rounded-full border px-3 py-1 text-[12px] font-medium ${
                    emailSource === "csv" ? "border-accent bg-accent/10 text-accent" : "border-line text-ink-2 hover:text-ink"
                  }`}
                >
                  Upload CSV
                </button>
              </div>
              {emailSource === "csv" ? (
                <div className="flex flex-col gap-2 rounded-control border border-line p-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <label className="inline-flex h-8 cursor-pointer items-center rounded-control border border-ink px-3 text-[12px] font-semibold text-ink hover:bg-page">
                      Upload Emails CSV
                      <input
                        key={emailFileKey}
                        type="file"
                        name="emails_file"
                        aria-label="Email ids file (.csv or .txt)"
                        accept=".csv,.txt,text/csv,text/plain"
                        className="sr-only"
                        disabled={locked}
                        onChange={(event) => void onEmailFile(event.target.files?.[0] ?? null)}
                      />
                    </label>
                    <a
                      href={EMAILS_TEMPLATE_HREF}
                      download="email-ids-template.csv"
                      className="inline-flex h-8 items-center rounded-control border border-line px-3 text-[12px] font-medium text-ink hover:bg-page"
                    >
                      Template
                    </a>
                  </div>
                  {emailFile ? (
                    <p className="text-[12px] text-ink-2">
                      <span className="font-medium text-ink">{emailFile.name}</span>: {countLabel(emailFile.matched, "account")} with a
                      valid session will run
                      {emailFile.notEligible.length ? `, ${formatCount(emailFile.notEligible.length)} without a valid session` : ""}
                      {emailFile.unmatched.length ? `, ${formatCount(emailFile.unmatched.length)} not found` : ""}.
                    </p>
                  ) : (
                    <p className="text-[12px] text-ink-2">One email id per row; any column. Only these accounts are used.</p>
                  )}
                  {emailFileError ? <p role="alert" className="text-[12px] text-ink">{emailFileError}</p> : null}
                </div>
              ) : null}
              {noneEligible ? (
                <Alert tone="warning" title="No account has a valid session">
                  A run needs a logged-in session per account.{" "}
                  <Link to="/sessions" className={LINK_STYLE}>
                    Generate sessions
                  </Link>{" "}
                  first — right now there is nothing to order with.
                </Alert>
              ) : null}

              <Checkbox
                name="smart_selection"
                label="Smart ID Selection"
                help="Try accounts that have never ordered first, then those that ordered longest ago, so buying is spread across accounts."
                checked={smartSelection}
                disabled={locked}
                onChange={(event) => {
                  setSmartSelection(event.target.checked);
                  setSubmitError(null);
                }}
              />

              <Disclosure title="Email ID Filters" defaultOpen>
                <FieldLookProvider value="stacked">
                  {emailSource === "csv" ? (
                    <p className="text-[12px] text-ink-2">The uploaded file decides the email ids; filters apply to All Email IDs.</p>
                  ) : (
                  <RunAccountPicker
                    state={accounts}
                    selection={selection}
                    onSelectionChange={setSelection}
                    filterStatus={["session_valid"]}
                    label="Accounts with a valid session"
                    checklists
                    disabled={locked}
                    emptyAction={
                      <Link to="/accounts" className={LINK_STYLE}>
                        Go to Accounts
                      </Link>
                    }
                  />
                  )}
                </FieldLookProvider>
                <div className="flex flex-col gap-2 rounded-control border border-dashed border-line bg-page p-3">
                  <FieldLookProvider value="stacked">
                    <Checkbox
                      name="exclude_ids"
                      label="Exclude IDs"
                      help="Based on recent orders of same platform: an account with an order in the window below is passed over."
                      checked={excludeDays.trim() !== ""}
                      disabled={locked}
                      onChange={(event) => {
                        setExcludeDays(event.target.checked ? "7" : "");
                        setSubmitError(null);
                      }}
                    />
                  </FieldLookProvider>
                  {excludeDays.trim() === "" ? null : (
                  <Field
                    label="Exclude IDs ordered in the last … days"
                    error={shown.exclude_days ?? (excludeDays.trim() ? errors.exclude_days : undefined)}
                    hint="Based on recent orders of same platform"
                    help="Optional. An account with an order placed (or sent and not confirmed) in that many days is passed over. Failed attempts do not count."
                  >
                    <NumberInput
                      name="exclude_ordered_days"
                      min={1}
                      max={365}
                      step={1}
                      placeholder="Off"
                      value={excludeDays}
                      disabled={locked}
                      onChange={(event) => {
                        setExcludeDays(event.target.value);
                        setSubmitError(null);
                      }}
                    />
                  </Field>
                  )}
                </div>
                <div className="grid gap-3 sm:grid-cols-2">
                  <Checkbox
                    name="use_supercoins"
                    label="Use Supercoins"
                    help="Takes each account's SuperCoins off the total on Flipkart's payment page. Accounts below the minimum (by their last read balance — Accounts → Read balances) are passed over."
                    checked={useSupercoins}
                    disabled={locked}
                    onChange={(event) => setUseSupercoins(event.target.checked)}
                  />
                  {useSupercoins ? (
                    <Field label="Minimum Supercoins" hint="Accounts with fewer are skipped">
                      <NumberInput
                        name="min_supercoins"
                        min={0}
                        step={1}
                        value={minSupercoins}
                        disabled={locked}
                        onChange={(event) => setMinSupercoins(event.target.value)}
                      />
                    </Field>
                  ) : (
                    <span />
                  )}
                  {SHOW_UNMAPPED_OPTIONS ? (
                    <Checkbox
                      name="apply_additional_offer"
                      label="Apply Additional Offer"
                      help="Recorded on the run: take an extra offer Flipkart shows at checkout."
                      checked={additionalOffer}
                      disabled={locked}
                      onChange={(event) => setAdditionalOffer(event.target.checked)}
                    />
                  ) : null}
                  {SHOW_UNMAPPED_OPTIONS ? <span /> : null}
                  <Checkbox
                    name="use_wallet"
                    label="Use Wallet Balance"
                    help="Takes each account's gift card / wallet balance off the total on the payment page."
                    checked={useWallet}
                    disabled={locked}
                    onChange={(event) => setUseWallet(event.target.checked)}
                  />
                </div>
              </Disclosure>

              <Disclosure title="Platform Unit Limits">
                <QuantityFields
                  part="facts"
                  draft={quantity}
                  errors={shownQuantity}
                  disabled={locked}
                  accountCount={accountsTried}
                  onChange={setQuantityField}
                />
                <Link to="/limits" className={`text-[12px] ${LINK_STYLE}`}>
                  Manage product limits
                </Link>
              </Disclosure>
            </FormSection>

            {/* --- 3. Payment (hidden in Mock: nothing is paid) ----------------- */}
            {mock ? null : (
            <FormSection title="3. Payment">
              <Field
                label="Payment mode"
                required
                help={
                  paymentMode === "cod"
                    ? "Nothing is charged up front — each order is paid on delivery, so no card is needed."
                    : usesGiftCards(paymentMode)
                      ? "Each order is paid with the account's gift cards from the sheet below" +
                        (paymentMode === "gv_card" ? ", the rest by card." : ".")
                      : paymentMode === "rtgs" || paymentMode === "imps" || paymentMode === "cod_card"
                        ? "Chosen on Flipkart's payment page by its name. An account whose payment page does not offer it gets no order (and is not charged)."
                        : "Each order is paid with a card from the sheet below."
                }
              >
                <Select
                  name="payment_mode"
                  value={paymentMode}
                  disabled={locked}
                  onChange={(event) => {
                    const mode = event.target.value as PaymentMode;
                    setPaymentMode(mode);
                    setSubmitError(null);
                    // Don't keep a card sheet around for a run that won't use one.
                    if (!usesGiftCards(mode)) {
                      setGiftFile(null);
                      setGiftFileKey((key) => key + 1);
                    }
                    if (!usesCard(mode)) {
                      setCardsFile(null);
                      setFileKey((key) => key + 1);
                      setFileNote(null);
                      setBankByHand(false);
                    }
                  }}
                >
                  {OFFERED_PAYMENT_MODES.map((value) => (
                    <option key={value} value={value}>
                      {PAYMENT_MODE_LABELS[value]}
                    </option>
                  ))}
                </Select>
              </Field>

              {usesGiftCards(paymentMode) ? (
                <div className="flex flex-col gap-2 rounded-control border border-line p-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <label className="inline-flex h-8 cursor-pointer items-center rounded-control border border-ink px-3 text-[12px] font-semibold text-ink hover:bg-page">
                      Upload Gift Cards CSV
                      <input
                        key={giftFileKey}
                        type="file"
                        name="gift_cards_file"
                        aria-label="Gift card sheet (.csv or .xlsx)"
                        accept=".csv,.xlsx,text/csv"
                        className="sr-only"
                        disabled={locked}
                        onChange={(event) => setGiftFile(event.target.files?.[0] ?? null)}
                      />
                    </label>
                    <a
                      href={GIFT_TEMPLATE_HREF}
                      download="gift-cards-template.csv"
                      className="inline-flex h-8 items-center rounded-control border border-line px-3 text-[12px] font-medium text-ink hover:bg-page"
                    >
                      Template
                    </a>
                    {giftFile ? <span className="text-[12px] text-ink-2">Ready to send: {giftFile.name}</span> : null}
                  </div>
                  <p className="text-[11.5px] text-muted">
                    One row per voucher: the account&rsquo;s email, the voucher number and its PIN (up to 15 per account).
                    Used for this run only and never saved.
                  </p>
                  {shown.gift_cards ? <p role="alert" className="text-[12px] text-ink">{shown.gift_cards}</p> : null}
                </div>
              ) : null}
              {!usesCard(paymentMode) ? (
                <p className="text-[12px] leading-snug text-ink-2">
                  No card is used and none needs uploading.
                  {paymentMode === "cod"
                    ? " A Cash-on-Delivery fee that pushes an order over the Final Amount Limit still stops that order."
                    : " The Final Amount Limit is checked per unit, on the order's cost before any balance pays part of it."}
                </p>
              ) : (
                <>
                  <div className="grid gap-3 sm:grid-cols-2">
                    <Field
                      label="Card Type"
                      hint="Blank uses every card in the sheet"
                      help="When the sheet has a Card Type column, only its rows of this type pay in this run. The type is also recorded on the run and shown on the Orders page."
                    >
                      <TextInput
                        name="card_type"
                        list={cardTypeListId}
                        autoComplete="off"
                        maxLength={64}
                        placeholder="Any card type"
                        value={cardType}
                        disabled={locked}
                        onChange={(event) => {
                          setCardType(event.target.value);
                          setSubmitError(null);
                        }}
                      />
                    </Field>
                    <datalist id={cardTypeListId}>
                      {CARD_TYPES.map((type) => (
                        <option key={type} value={type} />
                      ))}
                    </datalist>
                    <Field
                      label="Authentication"
                      required
                      help="Password: the tool types the password from the sheet's password column on the bank's page after Pay. OTP: a browser window opens on the computer the tool runs on and you type the bank's one-time code yourself."
                    >
                      <Select
                        name="authentication"
                        value={bankByHand ? "otp" : "password"}
                        disabled={locked || dryRun}
                        onChange={(event) => {
                          setBankByHand(event.target.value === "otp");
                          setSubmitError(null);
                        }}
                      >
                        <option value="password">Password</option>
                        <option value="otp">OTP (I type it)</option>
                      </Select>
                    </Field>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <label
                      className={`inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-control border border-ink px-3 text-[12px] font-semibold text-ink hover:bg-page ${
                        locked ? "pointer-events-none opacity-60" : ""
                      }`}
                    >
                      <svg viewBox="0 0 16 16" className="size-3.5" aria-hidden="true">
                        <path d="M8 11V3M5 6l3-3 3 3M3 13h10" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                      </svg>
                      Upload Cards CSV
                      <span className="sr-only"> (.xlsx or .csv)</span>
                      <FileInput
                        key={fileKey}
                        name="cards_file"
                        aria-label="Corporate cards (.xlsx or .csv)"
                        accept=".xlsx,.csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,text/csv"
                        disabled={locked}
                        className="sr-only"
                        onChange={(event) => onFileChange(event.target.files?.[0] ?? null)}
                      />
                    </label>
                    <a
                      href={CARD_TEMPLATE_HREF}
                      download="cards-template.csv"
                      className="inline-flex h-8 items-center gap-1.5 rounded-control border border-line px-3 text-[12px] font-medium text-ink hover:bg-page"
                    >
                      <svg viewBox="0 0 16 16" className="size-3.5" aria-hidden="true">
                        <path d="M8 3v8M5 8l3 3 3-3M3 13h10" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                      </svg>
                      Template
                    </a>
                    {sendableSheet ? (
                      <span className="text-[12px] text-ink-2">
                        Ready to send: <span className="font-medium text-ink">{sendableSheet.name}</span>{" "}
                        <span className="text-muted">({formatBytes(sendableSheet.size)})</span>
                      </span>
                    ) : null}
                  </div>
                  {cardsFileError ? (
                    <p role="alert" className="text-[12px] text-ink">
                      <span aria-hidden="true" className="mr-1.5 font-semibold" style={{ color: "var(--status-critical)" }}>
                        ✕
                      </span>
                      {cardsFileError}
                    </p>
                  ) : null}
                  <p className="text-[11.5px] leading-snug text-muted">
                    Cards are used for this run only and are never saved. One row per card: card number, expiry,
                    CVV and the account&rsquo;s email; a password column is typed on the bank&rsquo;s page after
                    Pay. Only the last four digits are shown here.
                    {fileNote ? <span className="mt-1 block text-ink">{fileNote}</span> : null}
                  </p>

                  {/* The pairing the run will use; gone with the file once a run starts. */}
                  {sendableSheet && runCount > 0 ? (
                    <FieldLookProvider value="stacked">
                      <CardPairingPreview file={sendableSheet} selection={selection} cardType={cardType} minRemaining={numberOrNull(cardRules.minRemaining)} />
                    </FieldLookProvider>
                  ) : null}

                  <Disclosure title="Cards Filters">
                    <fieldset className="flex flex-col gap-3" disabled={locked}>
                      <legend className="sr-only">Card filters</legend>
                      <div className="grid gap-3 sm:grid-cols-2">
                        <Field label="Card Eliminate Usage Days" hint="Exclude cards used in N days" help="A card that paid for an order in that many days is not used.">
                          <NumberInput name="card_cooldown_days" min={1} max={365} step={1} placeholder="Off" value={cardRules.cooldown} onChange={(event) => setCardRule("cooldown", event.target.value)} />
                        </Field>
                        <Field label="Card Sort Least Usage Days" hint="Prioritize least used in N days" help="Accounts whose card paid for the fewest orders in that many days go first.">
                          <NumberInput name="card_least_used_days" min={1} max={365} step={1} placeholder="Off" value={cardRules.leastUsed} onChange={(event) => setCardRule("leastUsed", event.target.value)} />
                        </Field>
                        <Field label="Fixed Max Card Usage (Max orders per card)" hint="Hard cap per card for this bulk order" help="A hard cap for a card the sheet gives to several accounts.">
                          <NumberInput name="card_max_uses" min={1} max={1000} step={1} placeholder="No cap" value={cardRules.maxUses} onChange={(event) => setCardRule("maxUses", event.target.value)} />
                        </Field>
                        <Field label="Exclude Cards with failures in last … days" hint="Drops cards that recently failed at payment" help="Only failures at the payment step count (declined, bank page, OTP), never an out-of-stock or address failure.">
                          <NumberInput name="card_exclude_failed_days" min={1} max={365} step={1} placeholder="Off" value={cardRules.failed} onChange={(event) => setCardRule("failed", event.target.value)} />
                        </Field>
                        <Field label="Minimum Remaining Amount (₹)" hint="HDFC child cards from CorpAssist" help="An HDFC child card with less than this left (opening amount less what it paid for here), or cancelled in CorpAssist, is not used. Cards CorpAssist does not know are not affected.">
                          <NumberInput name="card_min_remaining" min={1} step={1} placeholder="Off" value={cardRules.minRemaining} onChange={(event) => setCardRule("minRemaining", event.target.value)} />
                        </Field>
                      </div>
                      <div className="flex flex-col gap-2 rounded-control border border-line bg-page p-3">
                        <Checkbox
                          name="card_smart_cap"
                          label="Smart Max Card Usage"
                          help="Caps each card by its own history: a card that already paid for this many orders in the last 30 days is not used, and one that paid for fewer may pay only for the rest. If Fixed Max Card Usage is also set, the lower limit applies."
                          checked={smartCap}
                          onChange={(event) => setSmartCap(event.target.checked)}
                        />
                        {smartCap ? (
                          <Field label="Orders per card in 30 days" hint="From this tool's own order history">
                            <NumberInput
                              name="card_smart_max_uses"
                              min={1}
                              max={1000}
                              step={1}
                              value={cardRules.smart}
                              onChange={(event) => setCardRule("smart", event.target.value)}
                            />
                          </Field>
                        ) : null}
                      </div>
                      <div className="flex flex-col gap-2 border-t border-dashed border-line pt-3">
                        <Checkbox
                          name="card_exclude_codes"
                          label="Exclude Cards with specific failure errors"
                          help="Drops cards that recently failed an order with one of the selected errors. Uses the failure window above, or the last 30 days when it is blank."
                          checked={excludeCodesOn}
                          onChange={(event) => setExcludeCodesOn(event.target.checked)}
                        />
                        {excludeCodesOn ? (
                          <div className="grid gap-x-6 gap-y-1.5 px-1 sm:grid-cols-2">
                            {failureCodes.map((item) => (
                              <label key={item.code} className="flex items-center gap-2 text-[13px] text-ink">
                                <input
                                  type="checkbox"
                                  className="size-4 accent-accent"
                                  checked={excludeCodes.includes(item.code)}
                                  onChange={() =>
                                    setExcludeCodes((current) =>
                                      current.includes(item.code)
                                        ? current.filter((code) => code !== item.code)
                                        : [...current, item.code],
                                    )
                                  }
                                />
                                {item.name}
                              </label>
                            ))}
                          </div>
                        ) : null}
                      </div>
                      {shown.card_rules ?? (Object.values(cardRules).some((v) => v.trim()) || smartCap || excludeCodesOn ? errors.card_rules : undefined) ? (
                        <p className="text-[12px] text-critical">{shown.card_rules ?? errors.card_rules}</p>
                      ) : null}
                    </fieldset>
                  </Disclosure>

                  {bankByHand && !dryRun ? (
                    <Alert tone="info" title="You finish the bank's step">
                      A browser window opens on the computer the tool runs on, one account at a time, and shows the
                      card&rsquo;s details as they are typed. Leave it alone until the bank&rsquo;s page opens: the tool
                      types nothing there, so you type the card&rsquo;s password or OTP. The run below says how long
                      each bank page waits.
                    </Alert>
                  ) : null}
                </>
              )}
            </FormSection>
            )}

            {/* --- 4. Order Details ------------------------------------------ */}
            <FormSection title="4. Order Details">
              <QuantityFields
                part="limits"
                draft={quantity}
                errors={shownQuantity}
                disabled={locked}
                accountCount={accountsTried}
                onChange={setQuantityField}
                middle={
                  <Field
                    label="Target (max payable per unit)"
                    required={!noCap}
                    error={shown.target}
                    hint="Final Amount Limit"
                    help="Per unit: an order is placed only if its final amount — after the coupon, and counting any SuperCoins or wallet money used — divided by its units is at or below this. Over by even ₹0.01 and that one order fails; every other account continues."
                  >
                    <NumberInput
                      name="target"
                      min={0}
                      step="0.01"
                      placeholder={noCap ? "No cap" : "Enter a cap"}
                      value={target}
                      disabled={locked || noCap}
                      onChange={(event) => setTarget(event.target.value)}
                    />
                  </Field>
                }
              />

              <Checkbox
                name="no_cap"
                label="No price cap — order at any price"
                help="Only for a run that should have no ceiling at all. While this is on, the Final Amount Limit is cleared and turned off."
                checked={noCap}
                disabled={locked}
                onChange={(event) => {
                  const on = event.target.checked;
                  setNoCap(on);
                  // Cleared, not just hidden: a number left in a box that is
                  // turned off would read as a cap this run does not have.
                  if (on) setTarget("");
                  setSubmitError(null);
                }}
              />
              {noCap ? (
                <Alert tone="warning" title="No price cap: orders are placed at any amount">
                  Every order in this run is placed at whatever its final payable amount turns out to be,
                  however high. Turn off &ldquo;No price cap&rdquo; and enter a limit to put a hard ceiling on
                  each order.
                </Alert>
              ) : null}

              <FieldLookProvider value="stacked">
                <AddressGstPicker
                  value={place}
                  onChange={setPlace}
                  onResolved={onPlaceResolved}
                  disabled={locked}
                  ownGst={ownGst}
                />
              </FieldLookProvider>

              <div className="grid gap-3 sm:grid-cols-2">
                <Checkbox
                  name="gst_mandatory"
                  label="GST mandatory"
                  help="Every order must show this GST on Flipkart's checkout. An order whose checkout shows no GST is refused before payment instead of going ahead."
                  checked={gstMandatory}
                  disabled={locked}
                  onChange={(event) => {
                    setGstMandatory(event.target.checked);
                    setSubmitError(null);
                  }}
                />
                {SHOW_UNMAPPED_OPTIONS ? (
                  <Checkbox
                    name="flipkart_minutes"
                    label="Flipkart Minutes"
                    help="Marks the run as a Flipkart Minutes order: it is recorded on the run and shown with a Minutes tag on the Orders page."
                    checked={minutes}
                    disabled={locked}
                    onChange={(event) => setMinutes(event.target.checked)}
                  />
                ) : null}
                <Checkbox
                  name="use_address_phone"
                  label="Use default"
                  help="Every order carries the phone saved on the address instead of the account's. Off while phone numbers are typed below."
                  checked={phones.length === 0 && useAddressPhone}
                  disabled={locked || phones.length > 0}
                  onChange={(event) => {
                    setUseAddressPhone(event.target.checked);
                    setSubmitError(null);
                  }}
                />
              </div>

              <div className="-mb-1 flex justify-end">
                <button
                  type="button"
                  className="text-[12px] font-medium text-accent hover:underline disabled:opacity-50"
                  disabled={locked}
                  onClick={() =>
                    void listOrderPhones().then(
                      (rows) => {
                        const numbers = rows.filter((row) => row.active).map((row) => row.number);
                        setPhonesText(numbers.join(", "));
                        setSubmitError(numbers.length ? null : "No order phone number is marked in use (Platform Accounts & Configs → Order Phone Numbers).");
                      },
                      (err) => setSubmitError(errorMessage(err)),
                    )
                  }
                >
                  Use saved numbers
                </button>
              </div>
              <Field
                label="Delivery phone numbers"
                error={shown.address_phones ?? (phonesText.trim() ? errors.address_phones : undefined)}
                hint="Enter 10-digit numbers without country code. Separate with spaces or commas."
                help="Optional. The run's accounts use them in turn on the delivery address; leave blank to keep each account's own phone."
              >
                <TextInput
                  name="address_phones"
                  inputMode="tel"
                  autoComplete="off"
                  spellCheck={false}
                  placeholder="9876543210, 9876543211"
                  value={phonesText}
                  disabled={locked}
                  onChange={(event) => {
                    setPhonesText(event.target.value);
                    setSubmitError(null);
                  }}
                />
              </Field>
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="flex flex-col gap-2">
                  <Checkbox
                    name="coupons_on"
                    label="Coupon Codes"
                    help="Each code is claimed on every account just before its order (as on the Coupons page), so the checkout applies it. A code that does not take never stops the order."
                    checked={couponsOn}
                    disabled={locked}
                    onChange={(event) => setCouponsOn(event.target.checked)}
                  />
                  {couponsOn ? (
                    <Field label="Coupon Codes" hint="Separate with spaces or commas">
                      <TextInput name="coupon_codes" value={couponText} disabled={locked} onChange={(e) => setCouponText(e.target.value)} />
                    </Field>
                  ) : null}
                </div>
                {SHOW_UNMAPPED_OPTIONS ? (
                <div className="flex flex-col gap-2">
                  <Checkbox
                    name="rewards_on"
                    label="Reward IDs"
                    help="Recorded on the run with its orders."
                    checked={rewardsOn}
                    disabled={locked}
                    onChange={(event) => setRewardsOn(event.target.checked)}
                  />
                  {rewardsOn ? (
                    <Field label="Reward IDs" hint="Separate with spaces or commas">
                      <TextInput name="reward_ids" value={rewardText} disabled={locked} onChange={(e) => setRewardText(e.target.value)} />
                    </Field>
                  ) : null}
                </div>
                ) : null}
              </div>
            </FormSection>

            <div aria-live="polite">{submitError ? <Alert tone="error">{submitError}</Alert> : null}</div>
          </form>

          <OrderPreviewPanel
            className="xl:sticky xl:top-[72px]"
            runMode={runMode}
            aiSummary={
              ai
                ? `${aiDraft.minAttempts} per batch, scale up at ${aiDraft.scaleUpMode === "count" ? `${aiDraft.scaleUpCount} order(s)` : `${aiDraft.scaleUp}%`}, wait up to ${aiDraft.interval} min, for ${aiDraft.duration}${aiDraft.priceTracker ? " · price tracker" : ""}${aiDraft.rehearsal ? " (rehearsal)" : ""}`
                : null
            }
            preview={preview}
            totalUnits={plan.totalUnits}
            unitsPerOrder={plan.unitsPerOrder}
            accounts={accountsTried}
            seller={seller}
            defaultSeller={defaultSeller}
            target={target}
            noCap={noCap}
            qtyMode={qtyMode}
            paymentMode={paymentMode}
            cartLimit={quantity.cartAmountLimit}
            maxDeliveryDays={quantity.maxDeliveryDays}
            gstMandatory={gstMandatory}
            phones={phones.length}
            excludeDays={numberOrNull(excludeDays)}
            smartSelection={smartSelection}
            cardRules={[
              cardRules.cooldown.trim() ? `skip used in ${cardRules.cooldown} d` : "",
              cardRules.leastUsed.trim() ? `least used (${cardRules.leastUsed} d)` : "",
              cardRules.maxUses.trim() ? `max ${cardRules.maxUses}/card` : "",
              cardRules.failed.trim() ? `skip failed (${cardRules.failed} d)` : "",
              smartCap && cardRules.smart.trim() ? `smart ${cardRules.smart}/30 d` : "",
              excludeCodesOn && excludeCodes.length ? `skip ${excludeCodes.length} error type(s)` : "",
            ].filter(Boolean)}
            cardType={cardType.trim() || null}
            authentication={paymentMode === "card" ? (bankByHand ? "OTP (by hand)" : "Password") : null}
            useAddressPhone={useAddressPhone}
            address={placeDetails.address}
            gst={placeDetails.gst}
            limits={
              <LimitUsageSummary
                brand={quantity.brand}
                category={quantity.category}
                model={preview?.name ?? ""}
                seller={chosenSeller}
                selection={selection}
                requestedUnits={plan.requestedUnits}
              />
            }
            footer={
              <>
                <Button
                  type="submit"
                  form={formId}
                  variant="primary"
                  loading={submitting}
                  disabled={runActive || runCount === 0 || noAddress}
                  className="w-full"
                >
                  {submitLabel}
                </Button>
                <p className="text-[12px] leading-snug text-ink-2">{statusLine}</p>
              </>
            }
          />
        </div>
      </div>
    </FieldLookProvider>
  );
}

/** One numbered block of the form, as a card of its own. */
function FormSection({ title, lead, children }: { title: string; lead?: ReactNode; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-3 rounded-card border border-line bg-surface p-5">
      {lead}
      <h2 className="border-b border-line pb-2 text-[14px] font-semibold text-ink">{title}</h2>
      {children}
    </section>
  );
}

/** A collapsible strip ("Email ID Filters", "Cards Filters") with an accent edge. */
function Disclosure({ title, defaultOpen, children }: { title: string; defaultOpen?: boolean; children: ReactNode }) {
  return (
    <details open={defaultOpen} className="group rounded-control border border-line">
      <summary className="flex cursor-pointer list-none items-center justify-between gap-2 rounded-control border-l-[3px] border-accent bg-accent/5 px-3 py-1.5 text-[13px] font-medium text-accent [&::-webkit-details-marker]:hidden">
        {title}
        <svg viewBox="0 0 16 16" className="size-3.5 transition-transform group-open:rotate-180" aria-hidden="true">
          <path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        </svg>
      </summary>
      <div className="flex flex-col gap-3 p-3">{children}</div>
    </details>
  );
}

/** The card sheet's columns, as a file to fill in. */
const CARD_TEMPLATE_HREF = `data:text/csv;charset=utf-8,${encodeURIComponent(
  "Email,Card Number,Exp Month,Exp Year,CVV,Password,Card Type\n",
)}`;

/** The gift card sheet's columns, as a file to fill in. */
const GIFT_TEMPLATE_HREF = `data:text/csv;charset=utf-8,${encodeURIComponent("Email,Voucher Number,Voucher PIN\n")}`;

/** The Upload CSV template: one email id per row. */
const EMAILS_TEMPLATE_HREF = `data:text/csv;charset=utf-8,${encodeURIComponent("Email\n")}`;

/** The "Order execution version" choices (the server's EXECUTION_VERSIONS). */
const EXECUTION_VERSIONS: [string, string][] = [
  ["normal", "Normal Speed (Reliable)"],
  ["high", "High Speed (testing mode)"],
  // The "Higher MCC" variants are left out until they do something different
  // (audit of 19 Nov 2026: labels only, same speed, same flow).
];

/**
 * Options the form used to offer that no ordering step acts on yet — Apply
 * Additional Offer, Reward IDs and Flipkart Minutes were saved on the run and
 * then ignored (audit of 19 Nov 2026). Hidden until each is mapped on a live
 * checkout, so nobody believes a run used them.
 */
const SHOW_UNMAPPED_OPTIONS = false;

/** Card types offered in the Card Type box; any other can be typed. */
const CARD_TYPES = [
  "AMEX Corporate",
  "AXIS Corporate",
  "AXIS Physical Mastercard",
  "TRANSCORP",
  "HDFC Parent",
  "HDFC Virtual",
  "ICICI Corporate Virtual",
  "INDUSIND Physical Rupay",
  "INDUSIND Physical Visa",
  "INDUSIND VIRTUAL MASTERCARD",
  "1GB (P)",
  "RBL mastercard",
  "1GB (T)",
];

export default OrderForm;
