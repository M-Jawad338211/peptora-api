"""Validate a NOWPayments API key and our invoice payload against the live API.

This is the one thing the test suite cannot cover: whether NOWPayments accepts
the exact payload app/utils/nowpayments.py sends, and returns an invoice_url.

Creating an invoice moves no money and charges nobody — an unpaid invoice just
expires — so this is safe to run against a PRODUCTION key. Nothing is written
to our database; the invoice is created at NOWPayments and abandoned.

Usage:
    NOWPAYMENTS_API_KEY=xxx python -m scripts.check_nowpayments
    NOWPAYMENTS_API_KEY=xxx NOWPAYMENTS_API_URL=https://api-sandbox.nowpayments.io/v1 \
        python -m scripts.check_nowpayments
"""

import asyncio
import json
import os
import sys
import uuid

import httpx

API_URL = os.environ.get("NOWPAYMENTS_API_URL", "https://api.nowpayments.io/v1")
API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "")

MONTHLY_USD = float(os.environ.get("PRICE_MONTHLY_USD", 5))
ANNUAL_USD = float(os.environ.get("PRICE_ANNUAL_USD", 49))
LOW_FEE = ["usdttrc20", "trx", "ltc", "sol", "usdcsol", "maticmainnet", "bnbbsc"]

OK, BAD, WARN = "  ok  ", " FAIL ", " warn "


def line(tag: str, msg: str) -> None:
    print(f"[{tag}] {msg}")


async def main() -> int:
    if not API_KEY:
        line(BAD, "NOWPAYMENTS_API_KEY is not set")
        return 1

    line("  ..  ", f"using {API_URL}")
    headers = {"x-api-key": API_KEY, "Content-Type": "application/json"}
    failures = 0

    async with httpx.AsyncClient(timeout=30) as client:
        # 1. Is the API up at all? (No key required.)
        res = await client.get(f"{API_URL}/status")
        if res.status_code == 200:
            line(OK, f"/status -> {res.json()}")
        else:
            line(BAD, f"/status -> {res.status_code}")
            return 1

        # 2. Is the key valid? /currencies is deliberately NOT used here — it
        #    answers 200 for any key, including a made-up one, so it proves
        #    nothing. /min-amount is authenticated and 403s on a bad key.
        res = await client.get(
            f"{API_URL}/min-amount",
            headers=headers,
            params={"currency_from": "usdttrc20", "currency_to": "usdttrc20", "fiat_equivalent": "usd"},
        )
        if res.status_code in (401, 403):
            line(BAD, f"API key rejected ({res.status_code}) — check it was copied whole")
            return 1
        if res.status_code >= 400:
            line(BAD, f"/min-amount -> {res.status_code} {res.text[:200]}")
            return 1
        line(OK, "API key accepted")

        res = await client.get(f"{API_URL}/currencies", headers=headers)
        if res.status_code == 200:
            coins = res.json().get("currencies", [])
            missing = [c for c in LOW_FEE if c not in coins]
            if missing:
                line(WARN, f"not enabled on this account: {missing}")
                line(WARN, "enable them in the dashboard, or the $5 plan has no payable coin")
            else:
                line(OK, f"all {len(LOW_FEE)} low-fee coins available")

        # 3. Is $5 actually payable? This is the constraint that makes or
        #    breaks the monthly plan: NOWPayments sets a per-coin minimum to
        #    cover network fees, and on some chains it exceeds $5.
        for coin in LOW_FEE:
            res = await client.get(
                f"{API_URL}/min-amount",
                headers=headers,
                params={"currency_from": coin, "currency_to": coin, "fiat_equivalent": "usd"},
            )
            if res.status_code != 200:
                failures += 1
                line(BAD, f"min-amount {coin}: {res.status_code} {res.text[:120]}")
                continue
            data = res.json()
            fiat = data.get("fiat_equivalent")
            if fiat is None:
                line(WARN, f"min-amount {coin}: {json.dumps(data)}")
            elif float(fiat) > MONTHLY_USD:
                line(WARN, f"{coin}: minimum ${fiat} EXCEEDS the ${MONTHLY_USD:.0f} monthly price")
            else:
                line(OK, f"{coin}: minimum ${fiat} — monthly is payable")

        # 4. The real test: does our exact payload produce an invoice?
        for plan, amount in (("annual", ANNUAL_USD), ("monthly", MONTHLY_USD)):
            payload = {
                "price_amount": amount,
                "price_currency": "usd",
                "order_id": f"{uuid.uuid4()}:{plan}:{uuid.uuid4().hex[:12]}",
                "order_description": f"Peptora Pro — {plan} (connectivity check, do not pay)",
                "ipn_callback_url": "https://api.peptora.io/subscriptions/ipn",
                "success_url": "https://peptora.io/app/profile?checkout=success",
                "cancel_url": "https://peptora.io/app/pricing?checkout=cancelled",
                "is_fee_paid_by_user": True,
            }
            if plan == "monthly":
                payload["available_currencies"] = LOW_FEE

            res = await client.post(f"{API_URL}/invoice", headers=headers, json=payload)
            if res.status_code < 400 and res.json().get("invoice_url"):
                line(OK, f"{plan} invoice created -> {res.json()['invoice_url']}")
            else:
                failures += 1
                line(BAD, f"{plan} invoice -> {res.status_code} {res.text[:300]}")

    print()
    if failures:
        line(BAD, f"{failures} check(s) failed — do not enable payments yet")
        return 1
    line(OK, "all checks passed — safe to set NOWPAYMENTS_API_KEY in Railway")
    line("  ..  ", "the invoices above were never paid and will expire on their own")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
