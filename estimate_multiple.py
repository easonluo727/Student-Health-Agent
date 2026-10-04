import json
from openai import OpenAI
client = OpenAI()
request_text=input("Paste your request: ")
response = client.responses.create(
        model="gpt-5-nano",
        instructions="""
    You estiamte the cost of American student health insurance.

Extract the user's request and return only valid JSON.

Use exactly these fields:
- "service": string or null
- "network": "in-network" | "out-of-network" | null
- "remaining_deductible": number or null
- "allowed_amount": number or null

For "service", extract only the medical service itself.
Do not include network status, prices, deductible information,
or other modifiers in the service field.

Use null when information is not provided.
Do not invent information.


""",
    input=request_text
)

# dealing with aliasing
service_alias={
"blood test": "laboratory services",
"blood tests": "laboratory services",
"specialist visits": "specialist visit",
"lab test": "laboratory services",
"lab tests": "laboratory services",
}

raw_output=response.output_text
request_data=json.loads(raw_output)
print ("User request: ")
print(request_data)


requested_service = request_data["service"]
if requested_service is not None:
	requested_service = requested_service.lower().strip()
normalized_service = service_alias.get(requested_service, requested_service)
requested_network = request_data["network"]
if requested_network is not None:
        requested_network = requested_network.lower().strip()


with open("insurance_rules.json", "r") as file:
        stored_rules = json.load(file)

matched_rule = None
for rule in stored_rules:
	rule_service = rule["service"].lower().strip()
	rule_network = rule["network"].lower().strip()

	if (rule_service == normalized_service and rule_network == requested_network):
		matched_rule = rule
		break

if matched_rule == None:
	print ("Matching rule not found.")
	exit()

print("Matched rule: ")
print(matched_rule)


### Checkpoints
if matched_rule["out_of_network_covered"] == False:
	print ("Exact patient cost is unknown because the provider’s charge is unknown")
	exit()

## Calculation of cost
allowed_amount = request_data["allowed_amount"]
remaining_deductible = request_data["remaining_deductible"]
patient_pays_percentage = matched_rule["patient_pays_percentage"]
copay = matched_rule["copay_usd"]
deductible_applies = matched_rule["deductible_applies"]

if copay is not None:
    patient_cost = copay

elif patient_pays_percentage is None:
    print("The rule does not provide a copay or patient coinsurance percentage.")
    exit()

### Calculation of estimated cost when deductibel not applies
elif deductible_applies is False:
	if allowed_amount == None:
		print ("Allowed amount information is needed.")
		exit()
	patient_cost = allowed_amount * patient_pays_percentage / 100


### Calculation of estimated cost when deductible applies
elif deductible_applies is True:
	if allowed_amount == None or remaining_deductible == None:   
        	print ("Allowed amount or deductible information needed.")
        	exit()
	deductible_portion = min(
        	allowed_amount,
        	remaining_deductible
	)
        
	post_deductible_amount = allowed_amount - deductible_portion
	patient_cost = deductible_portion + post_deductible_amount * patient_pays_percentage / 100

else:
	print ("We need to know whether deductible applies.")
	exit()

print ("Patient cost: ")
print (patient_cost)
