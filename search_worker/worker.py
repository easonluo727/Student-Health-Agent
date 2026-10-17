"""Live insurer search worker. No LLM: Python picks results and links by keyword
score and reads facts with regular expressions.

Run:   uvicorn worker:app --port 8100        (from search_worker/)
API:   POST /search {"insurer": "Oscar Health"}  ->  text/event-stream

Events (see docs/frontend-protocol.md):
  search_step     {step_id, status, text, url, title, screenshot}
  search_summary  {insurer, status, facts, pages, message}

When a site blocks automated access the worker stops and says so; it never
tries to get around the block.
"""

import asyncio
import base64
import json
import re
from urllib.parse import quote_plus, urlparse

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright
from pydantic import BaseModel

VIEWPORT = {"width": 1280, "height": 800}
NAV_TIMEOUT_MS = 20000
HEALTH_WORDS = re.compile(r"health|insur|plan|coverage|medical", re.I)
BLOCK_SIGNS = re.compile(
    r"access denied|verify you are (a )?human|captcha|just a moment|unusual traffic|request blocked",
    re.I,
)
LINK_KEYWORDS = {
    "individual": 3, "family": 2, "california": 3, "summary of benefits": 4, "sbc": 3,
    "plans": 2, "plan": 1, "2026": 2, "shop": 1, "find a plan": 2, "coverage": 1,
    "premium": 2, "rates": 2,
}
LINK_PENALTIES = (
    "login", "log in", "sign in", "careers", "privacy", "terms", "broker", "employer", "provider",
    "facebook", "twitter", "linkedin", "instagram", "youtube",
    # Other lines of business: a student needs individual medical coverage.
    "dental", "vision", "medicare", "medicaid", "pet ", "life insurance", "supplemental", "travel",
)

app = FastAPI()


class SearchRequest(BaseModel):
    insurer: str


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def name_tokens(insurer):
    """'Oscar Health' -> ['oscar']: the distinctive words used to recognize the official site."""
    generic = {"health", "insurance", "healthcare", "inc", "the", "of", "company", "plan", "plans"}
    return [w for w in re.findall(r"[a-z0-9]+", insurer.lower()) if w not in generic] or [insurer.lower()]


COOKIE_REJECT = re.compile(r"^(reject( all| optional)?( cookies)?|decline( all)?|necessary (cookies )?only|only necessary)", re.I)


async def dismiss_cookie_banner(page):
    """Decline optional cookies if a consent banner covers the page."""
    try:
        buttons = page.get_by_role("button", name=COOKIE_REJECT)
        if await buttons.count():
            await buttons.first.click(timeout=2000)
            await page.wait_for_timeout(900)  # let the banner finish fading out
    except PlaywrightError:
        pass


HIGHLIGHT_JS = """el => {
    el.scrollIntoView({block: 'center'});
    const r = el.getBoundingClientRect();
    const box = document.createElement('div');
    box.id = '__agent_highlight';
    Object.assign(box.style, {
        position: 'fixed', left: (r.left - 6) + 'px', top: (r.top - 6) + 'px',
        width: (r.width + 12) + 'px', height: (r.height + 12) + 'px',
        border: '3px solid #e11d48', borderRadius: '8px', zIndex: 2147483647, pointerEvents: 'none'
    });
    document.getElementById('__agent_highlight')?.remove();
    document.body.appendChild(box);
}"""


async def highlight(page, element_handle):
    """Draw a red box over the chosen element so the screenshot shows the pick.
    An overlay, not a style on the element, so the site's CSS cannot hide it."""
    try:
        await element_handle.evaluate(HIGHLIGHT_JS)
        await page.wait_for_timeout(300)
    except PlaywrightError:
        pass


async def shot(page):
    data = await page.screenshot(type="jpeg", quality=55)
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


async def looks_blocked(page, response):
    if response is not None and response.status in (401, 403, 429, 503):
        return True
    try:
        title = await page.title()
        body = await page.inner_text("body")
    except PlaywrightError:
        return False
    # A bare error page with almost nothing on it is how some sites turn away bots.
    if re.search(r"\berror\b", title, re.I) and len(body) < 1500:
        return True
    return bool(BLOCK_SIGNS.search(title + " " + body[:2000]))


def money(text):
    return float(text.replace(",", ""))


def extract_facts(text):
    """Find premium, deductible and out-of-pocket max mentions. Returns label/value pairs."""
    facts = []
    flat = re.sub(r"\s+", " ", text)
    patterns = [
        ("Monthly premium", r"\$\s?([\d,]+(?:\.\d{2})?)\s?(?:/\s?mo(?:nth)?|per month|a month)"),
        ("Deductible", r"deductible[^$]{0,40}\$\s?([\d,]+)|\$\s?([\d,]+)\s?(?:annual |yearly )?deductible"),
        ("Out-of-pocket max", r"out[- ]of[- ]pocket (?:max(?:imum)?|limit)[^$]{0,40}\$\s?([\d,]+)|\$\s?([\d,]+)\s?out[- ]of[- ]pocket"),
    ]
    for label, pattern in patterns:
        values = []
        for match in re.finditer(pattern, flat, re.I):
            raw = next(g for g in match.groups() if g)
            value = money(raw)
            if value > 0 and value not in values:
                values.append(value)
            if len(values) == 3:
                break
        if values:
            shown = ", ".join(f"${v:,.0f}" if v == int(v) else f"${v:,.2f}" for v in values)
            facts.append({"label": label, "value": shown})
    metals = sorted({m.capitalize() for m in re.findall(r"\b(bronze|silver|gold|platinum)\b", flat, re.I)})
    if metals:
        facts.append({"label": "Metal tiers mentioned", "value": ", ".join(metals)})
    if re.search(r"\bcalifornia\b", flat, re.I):
        facts.append({"label": "Mentions California", "value": "Yes"})
    return facts


async def bing_results(page, query):
    """Return Bing's organic results. Bing is flaky with headless browsers (empty or
    hidden results on some loads), so try up to three loads before giving up."""
    for attempt in range(3):
        if attempt == 0:
            # Results stay hidden until Bing's stylesheets load: wait for "load".
            await page.goto(f"https://www.bing.com/search?q={quote_plus(query)}", wait_until="load", timeout=NAV_TIMEOUT_MS)
        else:
            await page.reload(wait_until="load", timeout=NAV_TIMEOUT_MS)
        try:
            await page.locator("li.b_algo h2 a").first.wait_for(state="visible", timeout=5000)
        except PlaywrightError:
            continue
        await page.wait_for_timeout(1500)  # results paint about a second after becoming visible
        # Bing sometimes hides the whole results column again (visibility: hidden) for
        # automated browsers. Treat that as a failed load and reload; never force it visible.
        hidden = await page.evaluate(
            "() => getComputedStyle(document.getElementById('b_content') || document.body).visibility !== 'visible'"
        )
        if hidden:
            continue
        return await page.eval_on_selector_all(
            "li.b_algo",
            """els => els.map((e, i) => ({
                i,
                title: (e.querySelector('h2')?.textContent || '').trim(),
                href: e.querySelector('h2 a')?.href || '',
                cite: (e.querySelector('cite')?.textContent || '').trim(),
                snippet: (e.querySelector('p')?.textContent || '').trim()
            }))""",
        )
    return []


async def run_search(insurer):
    tokens = name_tokens(insurer)
    pages = []

    async def step(step_id, status, text, page=None, url=None):
        data = {"step_id": step_id, "status": status, "text": text, "url": url, "title": None, "screenshot": None}
        if page is not None:
            data["url"] = url or page.url
            try:
                data["title"] = await page.title()
                data["screenshot"] = await shot(page)
            except PlaywrightError:
                pass
        return sse("search_step", data)

    def summary(status, message, facts=()):
        return sse("search_summary", {
            "insurer": insurer, "status": status, "message": message,
            "facts": list(facts), "pages": pages,
        })

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            # Keep Playwright's default user agent (it says HeadlessChrome): the worker does
            # not pretend to be a person's browser.
            context = await browser.new_context(viewport=VIEWPORT)
            page = await context.new_page()

            # 1. Search
            query = f"{insurer} individual health insurance plans California"
            yield await step("search", "running", f"Searching the web for “{query}”…")
            results = await bing_results(page, query)
            official = next(
                (r for r in results
                 if r["href"] and any(t in r["cite"].lower() for t in tokens)
                 and HEALTH_WORDS.search(r["title"] + " " + r["snippet"])),
                None,
            )
            if not results:
                yield await step("search", "failed", "The search engine didn't show results to an automated browser", page)
                yield summary("error", "The search engine didn't return results to my browser this time. Please try again in a moment.")
                return
            if official is None:
                yield await step("search", "failed", f"No official {insurer} site among {len(results)} results", page)
                yield summary("not_found", f"I couldn't find an official website for {insurer}.")
                return
            await highlight(page, (await page.query_selector_all("li.b_algo"))[official["i"]])
            yield await step("search", "done", f"Found the official site: {official['cite'].split(' ')[0]}", page)

            # 2. Open the official site
            yield await step("open", "running", f"Opening {official['title']}…")
            try:
                response = await page.goto(official["href"], wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                await page.wait_for_timeout(1500)
                await dismiss_cookie_banner(page)
            except PlaywrightError:
                yield await step("open", "failed", "The site didn't load in time", url=official["href"])
                yield summary("blocked", f"{insurer}'s website didn't load, so I couldn't check it.")
                return
            if await looks_blocked(page, response):
                yield await step("open", "failed", "The site blocked automated access — stopping here", page)
                yield summary("blocked", f"{insurer}'s website blocked automated access. I stopped instead of working around it.")
                return
            pages.append({"title": await page.title(), "url": page.url})
            yield await step("open", "done", f"Opened {await page.title()}", page)

            # 3. Score the links on the page and pick one
            yield await step("rank", "running", "Scoring the links on this page…")
            links = await page.eval_on_selector_all(
                "a[href]",
                """els => els.map((e, i) => ({
                    i, text: (e.innerText || e.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ').slice(0, 80),
                    href: e.href, visible: !!(e.offsetWidth || e.offsetHeight)
                }))""",
            )
            site = urlparse(page.url).netloc.split(".")[-2] if page.url else ""
            best, best_score = None, 0
            for link in links:
                text, href = link["text"], link["href"]
                if not text or not href.startswith("http") or not link["visible"]:
                    continue
                hay = f"{text} {href}".lower()
                if any(bad in hay for bad in LINK_PENALTIES):
                    continue
                score = sum(weight for word, weight in LINK_KEYWORDS.items() if word in hay)
                if site and site in urlparse(href).netloc:
                    score += 1
                if score > best_score:
                    best, best_score = link, score
            if best is None:
                yield await step("rank", "failed", f"None of the {len(links)} links looked like plan details", page)
                facts = extract_facts(await page.inner_text("body"))
                yield summary("partial", f"I opened {insurer}'s site but couldn't find a plan details page.", facts)
                return
            await highlight(page, (await page.query_selector_all("a[href]"))[best["i"]])
            yield await step("rank", "done", f"Picked “{best['text']}” (score {best_score}) from {len(links)} links", page)

            # 4. Open the chosen page and read the facts
            yield await step("extract", "running", f"Opening “{best['text']}” and reading prices…")
            try:
                response = await page.goto(best["href"], wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                await page.wait_for_timeout(2500)
                await dismiss_cookie_banner(page)
            except PlaywrightError:
                yield await step("extract", "failed", "That page didn't load in time", url=best["href"])
                yield summary("partial", f"I found {insurer}'s plans link but the page didn't load.")
                return
            if await looks_blocked(page, response):
                yield await step("extract", "failed", "This page blocked automated access — stopping here", page)
                yield summary("blocked", f"{insurer}'s plan page blocked automated access. I stopped instead of working around it.")
                return
            pages.append({"title": await page.title(), "url": page.url})
            facts = extract_facts(await page.inner_text("body"))
            prices = [f["label"].lower() for f in facts if f["label"] in ("Monthly premium", "Deductible", "Out-of-pocket max")]
            text = f"Read the page: found {', '.join(prices)}" if prices else "Read the page: no prices listed here"
            yield await step("extract", "done", text, page)
            if any(f["label"] in ("Monthly premium", "Deductible", "Out-of-pocket max") for f in facts):
                yield summary("found", f"Here's what {insurer}'s website shows.", facts)
            else:
                yield summary("partial", f"{insurer}'s plan page doesn't list prices without more details (for example a ZIP code), and I don't fill in forms.", facts)
        except (PlaywrightError, asyncio.TimeoutError) as error:
            yield sse("search_step", {"step_id": "error", "status": "failed", "text": f"Search stopped: {type(error).__name__}", "url": None, "title": None, "screenshot": None})
            yield summary("error", "The live search hit an error and stopped.")
        finally:
            await browser.close()


@app.post("/search")
async def search(request: SearchRequest):
    return StreamingResponse(
        run_search(request.insurer.strip()[:80]),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/health")
async def health():
    return {"ok": True}
