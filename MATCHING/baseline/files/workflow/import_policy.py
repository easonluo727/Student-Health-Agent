import base64
import json
import subprocess
from pathlib import Path
from openai import OpenAI

client = OpenAI()

# Get policy information
pdf_path = Path(
    input("PDF Path: ").strip()
).expanduser()

plan_name = (
    input("Plan Name: ")
    .strip()
    .lower()
    .replace(" ", "_")
)

try:
    start_page_number = int(
        input("Schedule of Benefits PDF Start Page: ")
    )
    end_page_number = int(
        input("Schedule of Benefits PDF End Page: ")
    )
except ValueError:
    print("PDF pages must be integers.")
    exit()

# Check whether the input is legitimate
if not pdf_path.exists():
    print("PDF does not exist:", pdf_path)
    exit()
if start_page_number < 1 or end_page_number < 1:
    print("Page numbers must be at least 1.")
    exit()
if start_page_number > end_page_number:
    print("End page must come after start page.")
    exit()

# Convert selected PDF pages into PNG images
image_prefix = (
    f"{plan_name}_policy_page_"
    f"{start_page_number}_to_{end_page_number}"
)
subprocess.run(
    [
        "pdftoppm",
        "-f", str(start_page_number),
        "-l", str(end_page_number),
        "-r", "300",
        "-png",
        str(pdf_path),
        image_prefix,
    ],
    check=True,
)
image_paths = list(
    Path(".").glob(f"{image_prefix}-*.png")
)
image_paths.sort(
    key=lambda path: int(
        path.stem.rsplit("-", 1)[1]
    )
)
if not image_paths:
    print("No PNG files were created.")
    exit()

print(f"Created {len(image_paths)} PNG files.")


# Store normalized rules from every page
all_normalized_rules = []

## Process each page image
for image_path in image_paths:
    page_number = int(
        image_path.stem.rsplit("-", 1)[1]
    )
    print(f"\nProcessing PDF page {page_number}...")
    image_bytes = image_path.read_bytes()
    encoded_image = base64.b64encode(
        image_bytes
    ).decode("ascii")

    ### First LLM call: extract the table rows

    response = client.responses.create(
        model="gpt-6-luna",
        instructions="""
Extract the visible benefit table into one JSON array. Return JSON only.

A physical row is one service cell and the benefit cells aligned with it
on the same horizontal band. Create exactly one object per physical row:

{
  "setting": "inpatient" | "outpatient" | "other" | null,
  "service": string,
  "preferred": string | null,
  "out_of_network": string | null
}

Rules:
- Process rows once, from top to bottom.
- Copy each cell completely and verbatim, including notes, limits,
  copays, percentages, deductible language, and qualifications.
- Use the nearest section heading above the row as setting.
- Keep repeated service names when they are separate physical rows.
- Never merge, split, infer, duplicate, or recreate rows.
- Never shift text between columns.
- Do not output headings, column labels, blank rows, or footers.
- Use null only when the corresponding cell is absent or unreadable.

Before answering, silently verify that every visible physical service row
appears exactly once and that every benefit remains in its original column.
""",
        input=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_image",
                        "image_url": (
                            f"data:image/png;base64,"
                            f"{encoded_image}"
                        ),
                    }
                ],
            }
        ],
    )

    raw_output = response.output_text
    insurance_rules = json.loads(raw_output)

    ### (Optional Block for batch) ###
    #batch_size = 5
    #normalizaed_rules = []

    ### (Optional Block for batch) ###

    print(
        f"Extracted {len(insurance_rules)} rows "
        f"from page {page_number}."
    )
    for index, row in enumerate(insurance_rules, start=1):
        print(
            index,
            "|",
            row.get("setting"),
            "|",
            repr(row.get("service")),
        )
   # =============================================================== #
    #exit()  # optional switch !!!
   # =============================================================== #

    ### Create network-specific entries with stable IDs
    benefit_entries = []
    entry_id = 0

    for row in insurance_rules:
        preferred_text = row.get("preferred")

        if preferred_text:
            entry_id += 1
            benefit_entries.append(
                {
                    "entry_id": entry_id,
                    "setting": row.get("setting"),
                    "service": row.get("service"),
                    "network": "in-network",
                    "benefit_text": preferred_text,
                    "source_text": json.dumps(
                        row,
                        ensure_ascii=False,
                    ),
                }
            )

        out_of_network_text = row.get(
            "out_of_network"
        )

        if out_of_network_text:
            entry_id += 1
            benefit_entries.append(
                {
                    "entry_id": entry_id,
                    "setting": row.get("setting"),
                    "service": row.get("service"),
                    "network": "out-of-network",
                    "benefit_text": out_of_network_text,
                    "source_text": json.dumps(
                        row,
                        ensure_ascii=False,
                    ),
                }
            )

    print(
        f"Created {len(benefit_entries)} "
        "network-specific entries."
    )

    ### Second LLM call: normalize the extracted rows
    normalization_instructions = """
Normalize each input entry. Return only one JSON array.

Return exactly one output object for every input object, in the same
order. Copy entry_id exactly. Never add, omit, merge, split, duplicate,
or reorder entries.

Each output object must contain exactly:
entry_id, copay_usd, plan_pays_percentage,
patient_pays_percentage, deductible_applies,
prior_authorization, coverage_status, needs_review.

Rules:
- Interpret payment fields only from benefit_text.
- Never infer payment information from service_text.
- "$X Copay" -> copay_usd = X.
- "X% of Allowed Amount" -> plan_pays_percentage = X and
  patient_pays_percentage = 100-X.
- "X% Coinsurance" -> patient_pays_percentage = X and
  plan_pays_percentage = 100-X.
- "after Deductible" -> deductible_applies = true.
- "not subject to Deductible" -> deductible_applies = false.
- "No Benefits" -> coverage_status = "not_covered".
- An explicit payment benefit or "Paid as any other Sickness"
  -> coverage_status = "covered".
- A reference without a usable benefit -> coverage_status = "unknown".
- prior_authorization is "required", "not_required",
  "may_be_required", or "not_stated".
- Use "not_stated" when prior authorization is not mentioned.
- Use null when benefit_text does not state a payment value.
- Store percentages as whole numbers, such as 80, not 0.8.
- needs_review = false for one clear copay or percentage rule.
- needs_review = true when service_text or benefit_text contains
  tiers, multiple payment methods, references, limits, conditions,
  contradictions, or ambiguous wording.
- When multiple values cannot fit unambiguously into the fields,
  use null for those values and set needs_review to true.

Before answering, silently verify that the output entry_id sequence
exactly matches the input entry_id sequence.
"""

    batch_size = 5
    normalized_rules = []

    for start_index in range(
        0,
        len(benefit_entries),
        batch_size,
    ):
        batch = benefit_entries[
            start_index:start_index + batch_size
        ]

        normalization_input = []

        for entry in batch:
            normalization_input.append(
                {
                    "entry_id": entry["entry_id"],
                    "service_text": entry["service"],
                    "benefit_text": entry["benefit_text"],
                }
            )

        print(
            f"Normalizing entries "
            f"{batch[0]['entry_id']} to "
            f"{batch[-1]['entry_id']}..."
        )

        normalization_response = client.responses.create(
            model="gpt-5-nano",
            instructions=normalization_instructions,
            input=json.dumps(
                normalization_input,
                ensure_ascii=False,
            ),
        )

        normalized_output = normalization_response.output_text

        normalized_batch = json.loads(
            normalized_output
        )

        input_ids = [
            entry["entry_id"]
            for entry in batch
        ]
        output_ids = [
            rule.get("entry_id")
            for rule in normalized_batch
        ]

        if output_ids != input_ids:
            print(
                "Normalization entry IDs do not match."
            )
            print("Expected IDs:", input_ids)
            print("Actual IDs:", output_ids)
            exit()

        normalized_by_id = {
            rule["entry_id"]: rule
            for rule in normalized_batch
        }

        for entry in batch:
            result = normalized_by_id[
                entry["entry_id"]
            ]

            normalized_rule = {
                "setting": entry["setting"],
                "service": (
                    entry["service"]
                    .splitlines()[0]
                    .lower()
                    .strip()
                ),
                "network": entry["network"],
                "copay_usd": result.get(
                    "copay_usd"
                ),
                "plan_pays_percentage": result.get(
                    "plan_pays_percentage"
                ),
                "patient_pays_percentage": result.get(
                    "patient_pays_percentage"
                ),
                "deductible_applies": result.get(
                    "deductible_applies"
                ),
                "prior_authorization": result.get(
                    "prior_authorization"
                ),
                "coverage_status": result.get(
                    "coverage_status"
                ),
                "source_text": entry["source_text"],
                "needs_review": result.get(
                    "needs_review"
                ),
            }

            normalized_rules.append(
                normalized_rule
            )

    ## Check Point: In-network and out-of-network rules should be paired
    expected_rule_count = 0
    for row in insurance_rules:
        if row.get("preferred"):
            expected_rule_count += 1
        if row.get("out_of_network"):
            expected_rule_count += 1

    actual_rule_count = len(normalized_rules)
    print("Expected normalized rules:", expected_rule_count)
    print("Actual normalized rules:", actual_rule_count)

    if actual_rule_count != expected_rule_count:
        print(
            f"WARNING: Page {page_number} may have missing rules."
        )
    else:
        print(f"Page {page_number} passed the rule-count check.")

    ## Add plan and source page info
    for rule in normalized_rules:
        rule["plan"] = plan_name
        rule["source_page"] = page_number

    ## Add this page's rules to the combined list
    all_normalized_rules.extend(
        normalized_rules
    )
    print(
        f"Added {len(normalized_rules)} normalized rules "
        f"from page {page_number}."
    )

# Print all rules after every page has been processed
print("\nAll normalized rules:")

print(
    json.dumps(
        all_normalized_rules,
        indent=2,
        ensure_ascii=False,
    )
)
# Save all pages into one JSON file
with open(
    "normalized_pdf_insurance_rules.json",
    "w",
    encoding="utf-8",
) as file:
    json.dump(
        all_normalized_rules,
        file,
        indent=2,
        ensure_ascii=False,
    )

print(
    "\nSaved "
    f"{len(all_normalized_rules)} rules to "
    "normalized_pdf_insurance_rules.json"
)
