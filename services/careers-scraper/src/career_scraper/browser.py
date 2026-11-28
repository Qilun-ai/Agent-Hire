"""Rendered inspection of public career pages.

Uses the stealth access layer (:mod:`career_scraper.access`) so a rendered
check has the same fingerprint, proxy and CAPTCHA handling as the collection
path. The returned dict keeps the historical keys (``url``, ``html``,
``requests``, ``responses``) and adds the access outcome.
"""
import asyncio
import hashlib
import json
import re

from . import access

_semaphore = asyncio.Semaphore(2)


async def render(url, output=None, *, load_media=False, solve_captcha=True,
                 referer=None, wait_ms=20_000):
    """Render ``url`` and capture public listing XHRs.

    The browser context is a coherent stealth profile; if the page settles on a
    challenge the CAPTCHA solver is used when a provider key is configured.
    """
    async with _semaphore:
        session = access.StealthSession()
        await session.__aenter__()
        try:
            page = session.page
            if not load_media:
                async def skip_media(route):
                    if route.request.resource_type in {"media"}:
                        await route.abort()
                    else:
                        await route.continue_()
                await page.route("**/*", skip_media)
            requests = []
            responses, pending = [], []
            page.on("request", lambda r: requests.append(r.url))

            async def capture(response):
                if response.request.resource_type not in {"xhr", "fetch"} or response.status != 200:
                    return
                if not re.search(r"job|position|career|recruit|listing", response.url, re.I):
                    return
                if not re.search(r"json", response.headers.get("content-type", "")):
                    return
                try:
                    responses.append({"url": response.url, "method": response.request.method,
                                      "request_body": response.request.post_data, "data": await response.json()})
                except Exception:
                    return

            page.on("response", lambda response: pending.append(asyncio.create_task(capture(response))))
            goto_kwargs = {"wait_until": "domcontentloaded", "timeout": 45_000}
            if referer:
                goto_kwargs["referer"] = referer
            response = await page.goto(url, **goto_kwargs)
            await session._humanize()
            html = await page.content()
            kind = access.detect_challenge(html, dict(response.headers) if response else None)
            solved = False
            if kind and solve_captcha and access.solver_configured():
                token = await access.solve_captcha(kind, page.url, html)
                if token:
                    solved = await session.inject_token(token)
            html = await session.settle(max_wait_ms=wait_ms)
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            # Trigger lazy career widgets by moving through the public page, without clicking forms.
            for _ in range(50):
                try:
                    at_end = await page.evaluate("""() => {
                        window.scrollBy(0, window.innerHeight * 0.8);
                        return window.scrollY + window.innerHeight >= document.body.scrollHeight;
                    }""")
                except Exception:
                    break
                await page.wait_for_timeout(200)
                if at_end:
                    break
            await page.wait_for_timeout(1200)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            final_html = await page.content()
            result = {
                "url": page.url,
                "html": final_html,
                "requests": requests,
                "responses": responses,
                "challenge": access.detect_challenge(final_html),
                "captcha_solved": solved,
                "access": access.attempt_metadata(
                    access.RenderedPage(url=url, final_url=page.url,
                                        status=response.status if response else None,
                                        html=final_html,
                                        challenge=access.detect_challenge(final_html),
                                        solved=solved),
                    url=url,
                ),
            }
            if output:
                directory = output / "browser"
                directory.mkdir(exist_ok=True, parents=True)
                digest = hashlib.sha256(url.encode()).hexdigest()
                (directory / f"{digest}.json").write_text(json.dumps(result))
            return result
        finally:
            await session.__aexit__()
