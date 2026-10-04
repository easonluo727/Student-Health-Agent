# Student Health Insurance Cost Estimator

An AI-assisted Python MVP that converts student health-insurance benefit tables into structured rules and uses those rules to estimate a patient's out-of-pocket cost.

The project separates language understanding from calculation:

- An OpenAI model extracts benefit-table rows and interprets user requests.
- JSON stores normalized insurance rules.
- Python performs rule matching and cost calculations.

This design keeps the final arithmetic deterministic and makes the result easier to inspect than asking an LLM to calculate everything directly.

## Current MVP

The current version supports the `prime_100` plan and can:

- Render a selected policy PDF page as an image.
- Extract rows from a Schedule of Benefits table while preserving its visual layout.
- Normalize extracted benefits into structured JSON.
- Interpret questions such as “How much will I pay for an in-network blood test?”
- Normalize service aliases such as `blood test` to `laboratory procedures`.
- Match rules by plan, service, and network.
- Calculate costs involving copays, coinsurance, allowed amounts, and remaining deductibles.
- Stop and request missing information instead of inventing values.
- Flag ambiguous or complex rules for manual review.
- Run the estimator manually through GitHub Actions.

## Example

Input:

```text
How much will I pay for an in-network blood test with a $100 allowed amount and $80 remaining deductible?
```

Matching benefit rule:

```json
{
  "plan": "prime_100",
  "service": "laboratory procedures",
  "network": "in-network",
  "copay_usd": null,
  "plan_pays_percentage": 80,
  "patient_pays_percentage": 20,
  "deductible_applies": true,
  "prior_authorization": "not_stated",
  "coverage_status": "covered"
}
```

Calculation:

```text
Deductible portion:        $80
Post-deductible amount:    $20
Patient coinsurance: 20% × $20 = $4
Estimated patient cost:    $84
```

## Workflow

### 1. Policy ingestion

`fourth_call.py` processes a selected policy page:

```text
Policy PDF
→ PDF page rendered as PNG
→ vision-based table extraction
→ normalized insurance rules
→ normalized_pdf_insurance_rules.json
```

The script currently targets a selected Schedule of Benefits page rather than automatically discovering every relevant page in a policy.

### 2. Cost estimation

`estimate_multiple.py` handles a user's question:

```text
User question
→ structured request
→ service alias normalization
→ rule matching
→ validation checkpoints
→ deterministic Python calculation
→ estimated patient cost
```

## Project Files

| File | Purpose |
| --- | --- |
| `fourth_call.py` | Renders a policy page, extracts benefit rows, and normalizes the results. |
| `estimate_multiple.py` | Interprets a user request, matches a stored benefit rule, and calculates estimated cost. |
| `normalized_pdf_insurance_rules.json` | Stores calculator-ready insurance rules extracted from the Prime 100 policy. |
| `requirements.txt` | Lists Python dependencies. |
| `.github/workflows/run-estimator.yml` | Runs the estimator manually through GitHub Actions. |

## Local Setup

### Requirements

- Python 3.12 or later
- An OpenAI API key
- Poppler (`pdftoppm`) for PDF rendering when running policy ingestion

### Installation

Clone the repository and enter the project directory:

```bash
git clone https://github.com/easonluo727/Student-Health-Agent.git
cd Student-Health-Agent
```

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Set the API key without placing it in source code:

```bash
export OPENAI_API_KEY="your-api-key"
```

Do not commit the API key or a `.env` file.

## Run the Estimator Locally

```bash
python estimate_multiple.py
```

Example request:

```text
How much will I pay for an in-network blood test with a $100 allowed amount and $80 remaining deductible?
```

## Run with GitHub Actions

Add `OPENAI_API_KEY` as a repository Actions secret, then:

1. Open the repository's **Actions** tab.
2. Select **Run insurance estimator**.
3. Click **Run workflow**.
4. Enter an insurance-cost question.
5. Open the `estimate` job and expand **Run estimator** to view the result.

The GitHub Actions workflow runs the estimator against the committed JSON rules. It does not ingest the policy PDF.

## Calculation Logic

When a deductible applies:

```python
deductible_portion = min(allowed_amount, remaining_deductible)
post_deductible_amount = allowed_amount - deductible_portion

patient_cost = (
    deductible_portion
    + post_deductible_amount * patient_pays_percentage / 100
)
```

When the deductible does not apply, the estimate is based on the applicable copay or patient coinsurance percentage.

## Current Limitations

- Only the Prime 100 sample plan is currently supported.
- Policy ingestion currently processes selected pages rather than the entire document automatically.
- Service matching relies on a small manually maintained alias dictionary.
- The user must provide the allowed amount when it is required for calculation.
- Complex benefits containing multiple interacting payment rules are flagged for manual review.
- The estimator does not verify claims, medical necessity, prior authorization approval, provider billing, or the insurer's final adjudication.
- GitHub Actions provides a developer-facing demonstration, not a public user interface.

## Planned Improvements

- Automatically identify Schedule of Benefits pages.
- Support multiple plans and policy documents.
- Expand and evaluate service-name normalization.
- Add structured-output validation and automated tests.
- Improve handling of combined copay and coinsurance rules.
- Add a web interface for PDF upload and cost questions.
- Preserve page-level citations for every extracted rule.

## Disclaimer

This project produces educational estimates only. It does not guarantee coverage or payment. Actual patient responsibility depends on the insurer's allowed amount, accumulated deductible, network status, exclusions, authorization requirements, claim processing, and the complete policy terms. Users should verify estimates with the insurer or plan administrator before making healthcare decisions.
