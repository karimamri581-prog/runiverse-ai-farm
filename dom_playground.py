async def strategy(page, ctx, log, S):
    # plan: click-cta | goal: connect wallet | target: button Connect Wallet
    last = "no candidate"
    ...
    cands = [
        ("role-name", lambda: page.get_by_role("button", name="Connect Wallet").first),
        ("text-exact", lambda: page.get_by_text("Connect Wallet", exact=True).first),
        ("has-text", lambda: page.locator("button", has_text="Connect Wallet").first),
    ]
    for how, mk in cands:
        try:
            loc = mk()
            ...
            await loc.click(timeout=4500)
            return {"ok": True, "how": "click:" + how, ...}
        except Exception as e:
            last = how + ": " + str(e)[:120]
    # fallback 1: real mouse at coordinates from the live probe
    # fallback 2: direct JS dispatch (ignores pointer interception)
