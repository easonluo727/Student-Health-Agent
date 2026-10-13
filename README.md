# Student Health Insurance Cost Estimator
A Python project that converts insurance policy PDFs into structured rules and uses them to estimate patient costs.

## Current progress
- Policy import: Converts selected PDF pages into images, extracts benefit tables with an LLM, and normalizes them into JSON. Supports multiple pages, network distinctions, source references, and batch validation. Extraction accuracy still needs improvement.
- Request interpretation — **in progress**: Match user descriptions to policy services and ask follow-up questions when information is ambiguous.
- Cost estimation — **in progress**: Update the existing calculator to use the imported rules, handle missing information, and flag complex benefits for review.

## Project Files

| File | Purpose |
| --- | --- |
| `import_policy.py` | Reads policy PDF pages, extracts benefit tables, normalizes them, and saves the insurance rules as JSON. |
| `interpret_request.py` | Understands the user’s request, matches candidate rules from the catalog, and returns structured information—including a clarification question if needed.  |
| `calculate_cost.py` | Returns calculated cost, missing fields, or a review status. |
| `estimate_pro.py` | Loads rules, calls `interpret_request.py`, print follow-up questions, calls `calculate_cost.py`, and prints the result. |
| `agent_tools.py` | Alternative to `estimate_pro.py`. Calls `calculate_cost.py`. |
| `agent.py`| Calls LLM to do what `interpret_request.py` used to do.|
| `.github/workflows/run-estimator.yml` | Runs the estimator manually through GitHub Actions. |
