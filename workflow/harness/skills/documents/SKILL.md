---
name: documents
description: Read any file the user uploads (PDF or photo) by looking at its contents, searching it and reading only the pages that matter; turn an insurance plan into rows Python can price.
---

An upload note in the user's message gives each file's `file_id`, name and size. Nothing has been read yet: you
decide what to look at, the way a careful researcher would.

### How to read a file

1. **Find out what it is.** Start with `document_overview` (page count and a table of contents from each page's
   heading). For a photo, or a PDF page with no text, use `view_page` with a question such as "What kind of
   document is this, and who issued it?".
2. **Find the part that answers the user.** Use `search_document` with a few alternative terms separated by `|`
   (for example `copay|coinsurance|you pay` or `deductible|out-of-pocket`), and the headings from the overview.
3. **Read only those pages** with `read_pages` (at most 5 at a time). When a table's text comes out jumbled, or a page
   has no text, look at it with `view_page` and ask a narrow question.
4. **Stop when you can answer.** Don't read a whole long document. Two or three searches and reads are usually enough.

Answer from what you read and cite the page ("page 19"). Quote amounts exactly as printed. If the file doesn't say,
say so; never fill a gap from memory.

### What to do with each kind of file

- **An insurance plan document** (Summary of Benefits and Coverage, evidence of coverage, benefit booklet,
  certificate): call `find_plan_match` first. If it matches, use that `plan_id` with the plans skill; the database's
  numbers win over your reading. If it doesn't match and the user wants costs or a comparison, use `extract_plan`:
  - `benefit_pages`: the pages of the cost-sharing table (an SBC's "What You Will Pay" chart; a booklet's "Schedule
    of Benefits" or "Cost Share" section). Check with a search or a read that they are the table, not the rules
    around it. At most 50.
  - `summary_pages`: the page(s) with the deductible and out-of-pocket limit (an SBC's page 1, "Important Questions").
  - `document_kind`: `sbc` for a Summary of Benefits and Coverage, otherwise `booklet`.
  - `plan_name`, `insurer`, `plan_year`: as printed in the document, or `null`.
  It takes about 10 seconds a page; tell the user it is working. The result is a new `plan_id`, unverified.
- **A medical bill or explanation of benefits (EOB)**: read it with `read_bill` (the bills skill), then check it
  with `audit_bill`.
- **An insurance card**: `view_page` for the insurer, plan name and group number; then `find_plan_match` can't
  help (no text), so match the names against the plan list yourself and confirm with the user.
- **Anything else** (a letter, a contract, a lab report): answer the user's question from what you read, and say
  plainly what this product can't do with it (it prices plans and checks bills).

Never compute a cost from a document yourself: costs come from the plans and bills tools.
