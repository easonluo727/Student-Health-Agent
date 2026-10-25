---
name: questionnaire
description: Ask the user for facts and preferences with a form card built from the question bank.
---

Use this skill whenever you need to know something about the user. It sends a form card into the chat. Each answer comes back tied to its field, and the numbers in it go straight to the calculator.

### When to ask

| The user… | Ask |
| --- | --- |
| asks how insurance works, or what you can do | nothing, just answer |
| wants their current plan evaluated | **B** (health and care use) and **C** (doctors and networks) |
| asks for a plan recommendation | **A** (about you), the **B** visit counts for the care they expect, and **E** (budget and priorities). The priority ratings rank the plans in Python, so ask all four together. |
| asks what one visit will cost | only the **D** questions the calculator still needs |

### How to ask

- Call `ask_questions` with the question IDs, a short `title`, and a one-sentence `intro` that says why you're asking.
- Choose only from the bank below. You can't write your own questions; if what you need isn't in the bank, ask in plain chat.
- Ask everything the next step needs in one form. Eight questions at most.
- `prefill` is only for values the user already stated in this conversation. The form shows them as "from your message, please confirm". Never prefill a guess.
- Don't ask again for something already answered. Earlier answers appear in messages that start with `[Questionnaire answers`.
- After calling `ask_questions`, stop. The user answers in the form, so don't repeat the questions in your reply.

### When answers come back

- Numbers from the questionnaire are trusted user inputs. Pass them to the calculator exactly as given.
- `unknown` means the user doesn't know. Don't stall: work with what you have, or show a few cases side by side.
- If the user closes the form without answering, continue with what you have.

### Question bank

{{QUESTION_BANK}}
