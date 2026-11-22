// Every Reason code (core/domain/decisions.py) in plain words, for a demo visitor watching
// Asha's assistant. tests/test_web_reason_codes.py fails if a code is missing here.
export const REASON_CODE_TEXT: Record<string, string> = {
  MANDATE_REVOKED: "Asha withdrew this permission, so nothing can be paid under it.",
  MANDATE_NOT_YET_VALID: "Asha's permission hasn't started yet.",
  MANDATE_EXPIRED: "Asha's permission has run out.",
  AGENT_NOT_ALLOWED: "Asha never authorised this assistant to pay.",
  AGENT_DISABLED: "This assistant has been switched off.",
  CURRENCY_MISMATCH: "The payment is in a different currency from Asha's permission.",
  MCC_HIGH_RISK: "This kind of shop (gambling, money transfer, cash-like) is never allowed.",
  MCC_NOT_ALLOWED: "Asha's permission doesn't cover this kind of shop.",
  PER_TXN_CAP_EXCEEDED: "The payment is over Asha's limit for a single payment.",
  MONTHLY_CAP_EXCEEDED: "The payment would take Asha over her monthly limit.",
  AMOUNT_ABOVE_STEP_UP_THRESHOLD: "Payments this large always need Asha's approval.",
  MONTHLY_CAP_NEAR: "With this payment Asha would be close to her monthly limit.",
  VELOCITY_HIGH: "The assistant has asked to pay many times in a few minutes.",
  MERCHANT_NEW: "Asha has never paid this shop before.",
  INTENT_MISSING: "The assistant didn't check the cart against what Asha asked for.",
  INTENT_MISMATCH: "The cart doesn't match what Asha asked for.",
  INTENT_UNKNOWN: "The cart check couldn't tell whether the cart matches what Asha asked for.",
  PRICE_DRIFT: "The price is higher than the total the assistant was quoted.",
};

export function reasonText(code: string): string {
  return REASON_CODE_TEXT[code] ?? code;
}

export const OUTCOME_TEXT: Record<string, string> = {
  allow: "Allowed: ShopBot may pay.",
  step_up: "Step up: Asha must approve first.",
  block: "Blocked: this payment can't go ahead, even with Asha's approval.",
};
