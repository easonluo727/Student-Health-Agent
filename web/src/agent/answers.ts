import type { Answer, Form } from "./protocol";

export const DONT_KNOW = "I don't know";

/** One line for a submitted questionnaire, shown on the card and as the user's message (also when a chat is reopened). */
export function summarizeAnswers(form: Form, answers: Record<string, Answer>): string {
  return form.questions.map(q => {
    const a = answers[q.field];
    if (!a || a.unknown) return DONT_KNOW;
    const label = (v: string) => q.options?.find(o => o.value === v)?.label ?? v;
    if (Array.isArray(a.value)) return a.value.map(label).join(", ") || DONT_KNOW;
    if (typeof a.value === "number") return q.kind === "weight" ? `${q.label}: ${a.value}/5` : `${a.value.toLocaleString("en-US")}${q.unit && !/budget/i.test(q.unit) ? ` ${q.unit}` : ""}`;
    return a.value ? label(a.value) : DONT_KNOW;
  }).join(" · ");
}
