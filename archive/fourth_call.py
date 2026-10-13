import json
import base64
import subprocess
from openai import OpenAI
from pathlib import Path
client = OpenAI()

#turn pdf into png
print ("reading pdf...")
subprocess.run(
    ["pdftoppm", "-f", "24", "-l", "24", "-r", "300",
     "-png", "-singlefile", "test_plan.pdf", "page24"],
    check=True,
)
print("PNG created")

#read the png, preserving location information
image_bytes = Path("page24.png").read_bytes()
encoded_image = base64.b64encode(image_bytes).decode("ascii")

response = client.responses.create(
	model="gpt-5-nano",
	instructions="""
Read the benefit tables in the image. Return only a JSON array.

Create one object for each actual medical service row, with exactly:
- "service": complete text from the left cell
- "preferred": text from the middle cell, or null if unreadable
- "out_of_network": text from the right cell, or null if unreadable

Copy the complete wording of each cell, including copays,
percentages, deductibles, limits, and qualifications.
Do not create duplicated service rows.
Do not calculate or interpret benefits.
Do not make objects for headings, column titles, or footers.
""",
    input=[{
        "role": "user",
        "content": [{
                "type": "input_image",
                "image_url": f"data:image/png;base64,{encoded_image}",
            },
        ],
    }],
)

raw_output=response.output_text
insurance_rule=json.loads(raw_output)
print(raw_output)
print(insurance_rule)

# =======================================================
# replaced block of dumping the transcript from pdf into json files
#check whether the output file exists
#rules_path = Path("insurance_rules_from_pdf2.json")
#if rules_path.exists():
#	with open("insurance_rules_from_pdf2.json", "r") as file:
#		old_rules = json.load(file)
#	old_rules.extend(insurance_rule)
#
#	with open("insurance_rules_from_pdf2.json", "w") as file:
#		json.dump(old_rules, file, indent=2)
#else:
#	with open("insurance_rules_from_pdf2.json", "w") as file:
#               json.dump(insurance_rule, file, indent=2)
# =======================================================

# normalize the extracted table rows

normalization_response = client.responses.create(
    model="gpt-5-nano",
    instructions="""
Convert the raw benefit rows into a JSON list with one object
for each service and network.

Fields:
plan, service, network, copay_usd, plan_pays_percentage,
patient_pays_percentage, deductible_applies,
prior_authorization, coverage_status, source_text, needs_review.

Rules:
- plan is "prime_100".
- preferred means "in-network".
- X% of Allowed Amount means plan pays X% and patient pays 100-X%.
- X% Coinsurance means the patient pays X%.
- after Deductible = true; not subject to Deductible = false.
- No Benefits = "not_covered"; otherwise covered when clearly stated.
- prior_authorization is "not_stated" unless stated.
- Use null rather than guessing.
- Preserve the complete original row in source_text.
- needs_review is true for tiers, multiple payment rules,
  references to other benefits, or ambiguous rules.
- Lowercase service and network.
- service must contain only the medical service name, normally the first line of the raw service field. Do not include conditions, limits, or explanatory notes.
- Store percentages as whole numbers: 80, not 0.8.
""",
    input=json.dumps(insurance_rule),
)

normalized_output = normalization_response.output_text
normalized_rules = json.loads(normalized_output)

print("\nNormalized rules:")
print(json.dumps(normalized_rules, indent=2))

with open(
    "normalized_pdf_insurance_rules.json",
    "w",
    encoding="utf-8",
) as file:
    json.dump(
        normalized_rules,
        file,
        indent=2,
        ensure_ascii=False,
    )

print("\nSaved to normalized_pdf_insurance_rules.json")
