interpret_request.py:
- match the user's request to policy rules
- if matched, return rule ID
- if several matched, return rule IDs and a clarification question

calculate_cost.py:
- check whether the selected rule supports calculation
- if supported, return estimated cost
- if missing information, return missing fields
- if not supported (needs review), return needs review
