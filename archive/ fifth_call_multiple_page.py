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
        model="gpt-5-nano",
        instructions="""
Read the benefit tables in the image. Return only a JSON array.

Create one object for each actual medical service row:
- "setting": "inpatient" | "outpatient" | "other" | null
- "service": complete text from the left cell
- "preferred": text from the middle cell, or null if unreadable
- "out_of_network": text from the right cell, or null if unreadable

Copy the complete wording of each cell, including copays,
percentages, deductibles, limits, and qualifications.
Do not omit a row because its service name appeared earlier.
The same service may appear under different settings.
Treat every visible table row as a separate row.
Do not create objects for headings, column titles, or footers.
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

    ### Second LLM call: normalize the extracted rows
    normalization_instructions = """
Convert the raw benefit rows into a JSON list with one object
for each service and network.

Fields:
setting, service, network, copay_usd, plan_pays_percentage,
patient_pays_percentage, deductible_applies,
prior_authorization, coverage_status, source_text, needs_review.

Rules:
- Preserve the setting from the raw row.
- setting must be "inpatient", "outpatient", "other", or null.
- Do not combine rules with the same service when their settings differ.
- preferred means "in-network".
- X% of Allowed Amount means plan pays X% and patient pays 100-X%.
- X% Coinsurance means the patient pays X%.
- after Deductible = true.
- not subject to Deductible = false.
- No Benefits = "not_covered".
- Otherwise use "covered" only when clearly stated.
- prior_authorization is "not_stated" unless stated.
- Use null rather than guessing.
- Preserve the complete original row in source_text.
- needs_review is true for tiers, multiple payment rules,
  references to other benefits, or ambiguous rules.
- Lowercase service and network.
- service must contain only the medical service name.
- Store percentages as whole numbers: 80, not 0.8.
- Create an in-network rule from every non-null preferred field.
- Create an out-of-network rule from every non-null out_of_network field.
"""


    normalization_response = client.responses.create(
        model="gpt-5-nano",
        instructions=normalization_instructions,
        input=json.dumps(insurance_rules),
    )

    normalized_output = normalization_response.output_text

    normalized_rules = json.loads(
        normalized_output
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
