"""Interactive coordinator. Run: python estimate.py

Requires interpret_request.py with the interface previously designed:
build_candidate_catalog(stored_rules, selected_plan)
interpret_request(request_text, candidate_catalog) -> dict
"""

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

from calculate_cost import calculate_cost
from interpret_request import build_candidate_catalog, interpret_request


RULES_PATH = Path(__file__).resolve().parent / "normalized_pdf_insurance_rules.json"


def ask(prompt):
    answer = input(prompt).strip()
    if answer.lower() in {"quit", "exit", "cancel"}:
        raise KeyboardInterrupt
    return answer


def ask_amount(field):
    labels = {
        "allowed_amount": "Insurer's allowed amount (not the provider's original charge)",
        "remaining_deductible": "Remaining deductible for this benefit/network",
    }
    while True:
        answer = ask(f"{labels[field]} in USD (or 'unknown'): ")
        if answer.lower() in {"unknown", "don't know", "not sure"}:
            return None
        try:
            value = Decimal(answer)
            if not value.is_finite() or value < 0:
                raise ValueError
        except (InvalidOperation, ValueError):
            print("Enter a nonnegative number such as 100.50, without a dollar sign.")
            continue
        return value


def choose_plan(rules):
    plans = sorted({r.get("plan") for r in rules if isinstance(r.get("plan"), str) and r["plan"]})
    if not plans:
        raise ValueError("The rules file has no plan names.")
    if len(plans) == 1:
        print("Plan:", plans[0])
        return plans[0]
    print("Available plans:", ", ".join(plans))
    while True:
        plan = ask("Plan name: ").lower().replace(" ", "_")
        if plan in plans:
            return plan
        print("Choose one of the listed plans.")


def main():
    with RULES_PATH.open(encoding="utf-8") as file:
        stored_rules = json.load(file)
    if not isinstance(stored_rules, list) or not stored_rules or not all(isinstance(r, dict) for r in stored_rules):
        raise ValueError("The rules file must contain a nonempty list of rule objects.")

    selected_plan = choose_plan(stored_rules)
    catalog = build_candidate_catalog(stored_rules, selected_plan)
    by_id = {candidate["id"]: candidate for candidate in catalog}
    if len(by_id) != len(catalog):
        raise ValueError("Candidate IDs must be unique.")

    print("Ask about one service on one occasion. Type 'quit' to stop.")
    request = ask("Your request: ")
    if not request:
        print("No request entered.")
        return

    # Keep the original request and every question/answer during clarification.
    history = [{"role": "user", "text": request}]
    for _ in range(6):
        request_data = interpret_request(json.dumps(history, ensure_ascii=False), catalog)
        if not isinstance(request_data, dict):
            raise ValueError("interpret_request() must return a dictionary.")
        status = request_data.get("match_status")
        if status == "matched":
            ids = request_data.get("candidate_ids")
            if not isinstance(ids, list) or len(ids) != 1 or ids[0] not in by_id:
                raise ValueError("A matched request must identify exactly one available rule.")
            candidate = by_id[ids[0]]
            matched_rule = stored_rules[candidate["rule_index"]]
            if matched_rule.get("plan") != selected_plan:
                raise ValueError("The selected rule belongs to a different plan.")
            break

        if status == "not_found":
            print("No matching rule was found in the imported rules. This does not prove noncoverage.")
            return
        if status not in {"needs_clarification", "unsupported_request"}:
            raise ValueError(f"Unexpected match status: {status!r}")
        question = request_data.get("clarification_question")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("The interpreter did not provide a clarification question.")
        print(question)
        reply = ask("Your answer: ")
        history.extend([
            {"role": "assistant", "text": question},
            {"role": "user", "text": reply},
        ])
    else:
        print("The request is still ambiguous. Please restart with one specific service and network.")
        return

    print("\nMatched:", matched_rule.get("service"), "|", matched_rule.get("network"), "|", matched_rule.get("setting"))
    print("Plan:", selected_plan, "| PDF page:", matched_rule.get("source_page", "unknown"))
    amounts = {
        "allowed_amount": request_data.get("allowed_amount"),
        "remaining_deductible": request_data.get("remaining_deductible"),
    }

    while True:
        result = calculate_cost(matched_rule, **amounts)
        if result["status"] != "needs_information":
            break
        print(result["message"])
        for field in result["missing_fields"]:
            value = ask_amount(field)
            if value is None:
                print("No estimate calculated. Still needed:", ", ".join(result["missing_fields"]))
                return
            amounts[field] = value

    print("\nStatus:", result["status"])
    print(result["message"])
    if result["patient_cost"] is not None:
        print(f"Estimated patient cost: ${result['patient_cost']:.2f}")
    elif result["status"] in {"needs_review", "not_covered"}:
        print("Source:", matched_rule.get("source_text", "Unavailable"))


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nStopped. No estimate calculated.")
    except Exception as error:
        # At the CLI boundary, abort rather than continue with invalid/API data.
        print(f"Unable to complete the estimate: {type(error).__name__}: {error}")
        raise SystemExit(1)
