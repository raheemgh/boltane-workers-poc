"""
Stage 4 — diagnostic probe (NOT a pass/fail test). Talks to OpenRouter directly,
bypassing ai_client, to explain what the real run showed with deepseek/deepseek-v4-flash:
replies cut off mid-word, "GRACEFUL" failures, and totals far above the 300-token cap.

For each call it prints: finish reason, prompt / completion / reasoning tokens as reported
by OpenRouter, visible characters, and reasoning characters. Run from cf-worker:

    PYTHONPATH=tooling/local-test-support:src python probe_stage4_tokens.py

About 10 calls on the paid model; the two long-answer calls can take a minute or more.
The key is read from .dev.vars and never printed. Nothing here changes any code.
"""

import asyncio
import json
import os
import sys
import time

import httpx

import test_stage4_real as real  # loads .dev.vars; reuses the sample prompts/messages

MODEL = "deepseek/deepseek-v4-flash"
URL = "https://openrouter.ai/api/v1/chat/completions"
SYSTEM = real.REAL_SYSTEM_PROMPT


async def one(label, message, max_tokens, model=MODEL, extra=None, show_text=False):
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": message}],
        "max_tokens": max_tokens, "stream": True, "stream_options": {"include_usage": True},
    }
    body.update(extra or {})
    headers = {"Authorization": f"Bearer {os.environ['OPENROUTER_DEFAULT_KEY']}", "Content-Type": "application/json"}
    content = reasoning = ""
    finish = usage = None
    http = "?"
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            async with client.stream("POST", URL, headers=headers, json=body) as resp:
                http = resp.status_code
                if resp.status_code != 200:
                    await resp.aread()
                    print(f"{label:<28} HTTP {resp.status_code}: {resp.text[:200]}")
                    return None
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:") or line.endswith("[DONE]"):
                        continue
                    try:
                        event = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if event.get("error"):
                        print(f"{label:<28} error event inside the stream: {str(event['error'])[:200]}")
                    for choice in event.get("choices") or []:
                        delta = choice.get("delta") or {}
                        content += delta.get("content") or ""
                        reasoning += delta.get("reasoning") or delta.get("reasoning_content") or ""
                        finish = choice.get("finish_reason") or finish
                    usage = event.get("usage") or usage
    except httpx.HTTPError as exc:
        print(f"{label:<28} network error: {exc}")
        return None
    usage = usage or {}
    details = usage.get("completion_tokens_details") or {}
    row = dict(label=label, max_tokens=max_tokens, finish=finish, prompt=usage.get("prompt_tokens"),
               completion=usage.get("completion_tokens"), reasoning_tokens=details.get("reasoning_tokens"),
               content_chars=len(content), reasoning_chars=len(reasoning), seconds=round(time.perf_counter() - started, 1))
    print(f"{label:<28} max={max_tokens:<5} finish={str(finish):<8} prompt={row['prompt']!s:<5} "
          f"completion={row['completion']!s:<5} reasoning_tok={row['reasoning_tokens']!s:<5} "
          f"visible_chars={row['content_chars']:<5} reasoning_chars={row['reasoning_chars']:<5} {row['seconds']}s")
    if show_text and content:
        print("      reply starts:", content[:140].replace("\n", " "))
    return row


async def main():
    real._check_key()
    m = real.MESSAGES
    rows = {}
    plan = [
        ("baseline '.' (input size)", ".", 20),
        ("M09 input size", m["M09"], 20),
        ("M01 cap 300", m["M01"], 300), ("M01 cap 1500", m["M01"], 1500),
        ("M02 cap 300", m["M02"], 300), ("M02 cap 1500", m["M02"], 1500),
        ("M10 cap 300", m["M10"], 300), ("M10 cap 1500", m["M10"], 1500),
        ("M09 cap 300", m["M09"], 300),
    ]
    for label, message, cap in plan:
        rows[label] = await one(label, message, cap)
        await asyncio.sleep(1.0)

    print("\nreading the numbers:")
    base, m09 = rows["baseline '.' (input size)"], rows["M09 input size"]
    if base and m09 and base["prompt"] and m09["prompt"]:
        m09_tokens = m09["prompt"] - base["prompt"]
        print(f" - M09 ({len(m['M09'])} Arabic chars) costs about {m09_tokens} input tokens "
              f"= {m09_tokens / len(m['M09']):.2f} tokens per Arabic char; the engine's estimate_tokens() says "
              f"{-(-len(m['M09']) // 4)} (it assumes 4 chars per token)")
    for label in ("M01 cap 1500", "M02 cap 1500", "M10 cap 1500"):
        r = rows.get(label)
        if r and r["completion"] is not None and r["content_chars"]:
            visible = r["completion"] - (r["reasoning_tokens"] or 0)
            print(f" - {label}: {r['content_chars']} visible Arabic chars used {visible} visible tokens "
                  f"= {visible / r['content_chars']:.2f} tokens per char; reasoning tokens: {r['reasoning_tokens']}")
    print(" - if 'completion' exceeds the cap while finish=length/stop, reasoning tokens are billed on top of "
          "the visible-answer cap; if 'visible_chars' is 0 with reasoning_chars > 0, the cap was spent thinking")


async def variants():
    """Can the reasoning be switched off or turned down, and does a non-reasoning model do better?
    Every call uses the production output cap (300). Messages = the ones that failed or got cut."""
    real._check_key()
    m = real.MESSAGES
    messages = [("M01", m["M01"]), ("M02", m["M02"]), ("M09", m["M09"]), ("M10", m["M10"])]
    variant_list = [
        ("V4 Flash, reasoning off", MODEL, {"reasoning": {"enabled": False}}),
        ("V4 Flash, effort low", MODEL, {"reasoning": {"effort": "low"}}),
        ("DeepSeek V3 (deepseek-chat)", "deepseek/deepseek-chat", None),
    ]
    summary = {}
    for vname, model, extra in variant_list:
        for mid, text in messages:
            row = await one(f"{vname} / {mid}", text, 300, model=model, extra=extra)
            summary.setdefault(vname, []).append(row)
            await asyncio.sleep(1.0)
    print("\nsummary at cap 300 (4 messages each):")
    for vname, rows in summary.items():
        ok = [r for r in rows if r]
        with_text = sum(1 for r in ok if r["content_chars"] > 0)
        cut = sum(1 for r in ok if r["finish"] == "length")
        thinking = sum((r["reasoning_tokens"] or 0) for r in ok)
        print(f" - {vname}: answered {with_text}/4, hit the cap {cut}/4, reasoning tokens total {thinking}, "
              f"failed requests {4 - len(ok)}")
    print("(a 'failed request' above printed its HTTP error earlier in the output — send that line too)")


# Candidates taken from https://openrouter.ai/collections/free-models (checked 2026-10-13).
# Left out on purpose: Poolside Laguna and Liquid LFM (their pages say free usage may be used for
# training), coding-only models, rerankers, and anything labelled reasoning-first.
FREE_CANDIDATES = [
    "nvidia/nemotron-3.5-lightning:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "dots-studio/dots-3-note-preview:free",
    "thinkingmachines/inkling:free",
    "inclusionai/ling-3.1-flash",   # no ":free" in the slug although listed at $0 — check it stays free
]


async def free():
    """Which free models can answer at the production cap (300), and can their thinking be switched off?
    One question (M02) per model, twice: default and reasoning off. Spaced out for the free-tier rate limit.
    Uses the free quota (an account without credit gets only ~50 free requests a day)."""
    real._check_key()
    question = real.MESSAGES["M02"]
    summary = {}
    for model in FREE_CANDIDATES:
        for vname, extra in (("default", None), ("reasoning off", {"reasoning": {"enabled": False}})):
            row = await one(f"{model.split('/')[-1]} / {vname}", question, 300, model=model, extra=extra, show_text=True)
            summary[(model, vname)] = row
            await asyncio.sleep(4.0)
    print("\nsummary (answered = visible text came back at cap 300):")
    for (model, vname), row in summary.items():
        verdict = "request failed (see the HTTP line above)" if not row else (
            f"answered, {row['content_chars']} chars, finish={row['finish']}, reasoning tokens={row['reasoning_tokens']}"
            if row["content_chars"] else f"EMPTY (reasoning tokens={row['reasoning_tokens']}, finish={row['finish']})")
        print(f" - {model} [{vname}]: {verdict}")


async def model_check(slug):
    """One model, four messages (greeting, menu question, order with a Syrian phone number, long
    detailed request), each with default settings and with reasoning off, at the production cap (300).
    Replies are previewed so the Arabic can be judged by eye."""
    real._check_key()
    m = real.MESSAGES
    messages = [("M01", m["M01"]), ("M02", m["M02"]), ("M03", m["M03"]), ("M10", m["M10"])]
    summary = []
    for vname, extra in (("default", None), ("reasoning off", {"reasoning": {"enabled": False}})):
        for mid, text in messages:
            row = await one(f"{slug.split('/')[-1]} / {vname} / {mid}", text, 300, model=slug, extra=extra, show_text=True)
            summary.append((vname, mid, row))
            await asyncio.sleep(1.5)
    print(f"\nsummary for {slug} at cap 300:")
    for vname in ("default", "reasoning off"):
        rows = [(mid, r) for v, mid, r in summary if v == vname]
        answered = sum(1 for _, r in rows if r and r["content_chars"] > 0)
        cut = sum(1 for _, r in rows if r and r["finish"] == "length")
        secs = [r["seconds"] for _, r in rows if r]
        print(f" - {vname}: answered {answered}/4, hit the cap {cut}/4, "
              f"failed requests {sum(1 for _, r in rows if not r)}, "
              f"seconds per call {min(secs) if secs else '-'}-{max(secs) if secs else '-'}")


if __name__ == "__main__":
    mode = sys.argv[1:]
    if len(mode) == 2 and mode[0] == "model":
        asyncio.run(model_check(mode[1]))
    else:
        asyncio.run(variants() if mode == ["variants"] else free() if mode == ["free"] else main())
