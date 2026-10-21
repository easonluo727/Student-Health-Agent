"""Calculate one service occurrence using an already matched policy rule.

Percentages use 0–100 units. This module does not read files or call an LLM.
Supports copay-only, coinsurance-only, and copay-then-coinsurance (no deductible) benefits.
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def _result(status, message, patient_cost=None, missing_fields=None):
    return {
        "status": status,
        "patient_cost": patient_cost,
        "missing_fields": missing_fields or [],
        "message": message,
    }


def _number(value, name, maximum=None):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number, not a Boolean.")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a valid number.") from None
    if not number.is_finite() or number < 0:
        raise ValueError(f"{name} must be finite and nonnegative.")
    if maximum is not None and number > maximum:
        raise ValueError(f"{name} must be between 0 and {maximum}.")
    return number


def calculate_cost(matched_rule, allowed_amount=None, remaining_deductible=None):
    """Return status, patient_cost, missing_fields, and message every time.

The caller must select the correct plan, service, setting, and network first.
An estimate excludes balance billing and out-of-pocket maximum adjustments.
It does not verify authorization or other policy eligibility requirements.
"""
    if not isinstance(matched_rule, dict):
        return _result("needs_review", "The matched rule must be a dictionary.")

    coverage = matched_rule.get("coverage_status")
    if coverage == "not_covered":
        return _result("not_covered", "This benefit is not covered. No patient cost was calculated.")
    if coverage != "covered":
        return _result("needs_review", "Coverage is unknown or invalid.")
    # Require an explicit False; missing flags must not silently pass.
    if matched_rule.get("needs_review") is not False:
        return _result("needs_review", "This policy rule requires review before calculation.")

    deductible = matched_rule.get("deductible_applies")
    if deductible is not True and deductible is not False:
        return _result("needs_review", "The policy must clarify whether the deductible applies.")

    try:
        copay = matched_rule.get("copay_usd")
        patient = matched_rule.get("patient_pays_percentage")
        plan = matched_rule.get("plan_pays_percentage")
        copay = None if copay is None else _number(copay, "copay_usd")
        patient = None if patient is None else _number(patient, "patient_pays_percentage", 100)
        plan = None if plan is None else _number(plan, "plan_pays_percentage", 100)
    except ValueError as error:
        return _result("needs_review", str(error))

    if patient is not None and plan is not None and patient + plan != 100:
        return _result("needs_review", "Plan and patient percentages do not add up to 100.")

    # Never assume that a listed copay is the entire cost of a combined benefit.
    if copay is not None and patient not in (None, Decimal(0)):
        # Copay first, then coinsurance on the rest: $200 visit, $25 + 20% -> 25 + 175 * 20%.
        if deductible:
            return _result("needs_review", "Copay plus coinsurance subject to a deductible is not supported yet.")
        if allowed_amount is None:
            return _result("needs_information", "More information is needed to calculate this benefit.",
                           missing_fields=["allowed_amount"])
        try:
            amount = _number(allowed_amount, "allowed_amount")
        except ValueError as error:
            return _result("invalid_input", str(error))
        cost = min(copay, amount) + max(amount - copay, Decimal(0)) * patient / 100
    elif copay is not None:
        if plan not in (None, Decimal(100)):
            return _result("needs_review", "This rule combines a copay and coinsurance.")
        if deductible:
            # Copay after the deductible: the full amount up to the remaining deductible, then the copay
            # (never more than what is left of the bill). $300 visit, $100 deductible left, $60 copay -> 160.
            required = {"allowed_amount": allowed_amount, "remaining_deductible": remaining_deductible}
            missing = [name for name, value in required.items() if value is None]
            if missing:
                return _result("needs_information", "More information is needed to calculate this benefit.",
                               missing_fields=missing)
            try:
                amount = _number(allowed_amount, "allowed_amount")
                left = _number(remaining_deductible, "remaining_deductible")
            except ValueError as error:
                return _result("invalid_input", str(error))
            portion = min(amount, left)
            cost = portion + min(copay, amount - portion)
        else:
            # A copay never exceeds what the service costs: $50 lab copay on a $9 test -> 9 (Covered California
            # 2026 benefit designs, endnote 6; UC SHIP booklet: "the lesser of the amount shown ... or the amount
            # the Provider charges"). Without a price the listed copay is the most the user pays.
            cost = copay
            if allowed_amount is not None:
                try:
                    cost = min(copay, _number(allowed_amount, "allowed_amount"))
                except ValueError as error:
                    return _result("invalid_input", str(error))
    else:
        if patient is None:
            return _result("needs_review", "The rule does not specify a usable patient percentage or copay.")

        required = {"allowed_amount": allowed_amount}
        if deductible:
            required["remaining_deductible"] = remaining_deductible

        missing = [name for name, value in required.items() if value is None]
        if missing:
            return _result("needs_information", "More information is needed to calculate this benefit.", missing_fields=missing)
        try:
            values = {name: _number(value, name) for name, value in required.items()}
        except ValueError as error:
            return _result("invalid_input", str(error))

        amount = values["allowed_amount"]
        portion = min(amount, values["remaining_deductible"]) if deductible else Decimal(0)
        cost = portion + (amount - portion) * patient / 100

    cost = float(cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return _result(
        "estimated",
        "Estimate for one service occurrence under this rule; excludes balance billing "
        "and out-of-pocket maximum adjustments. Not a coverage guarantee.",
        patient_cost=cost,
    )
