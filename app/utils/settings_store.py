"""Accessor for the single app_settings row.

Billing configuration lives in the database rather than in config.py because
bank accounts get frozen and prices change, and neither should require a
redeploy and a cold start. config.py still supplies the seed values used the
first time the row is created.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings as env
from app.models import AppSettings

logger = logging.getLogger("peptora.settings")

DEFAULT_INSTRUCTIONS_MD = """\
### Before you transfer

- Send the **exact amount** shown above, in the currency listed. A short
  amount is the single most common reason a payment cannot be matched.
- Use the reference format shown in the bank details, if one is given —
  it is what lets us find your transfer without asking you follow-up
  questions.
- Take a screenshot of the confirmation screen, or save the PDF receipt,
  before you close your banking app.

### After you transfer

1. Come back to this page.
2. Fill in the transaction reference, the amount and date you sent, and
   who the transfer was sent from (if it was not this account).
3. Attach the screenshot or PDF. JPG, PNG, WebP or PDF, up to 8 MB.
4. Submit. You will get an email confirming we received it.

### What happens next

A person — not a computer — checks every payment against your receipt, so
activation is not instant. Once approved, your licence goes live
immediately and you get an email; nothing else to enter, nothing to
refresh. If anything about your submission needs fixing, we will tell you
exactly what and let you send corrected details.

### Cannot transfer directly?

Email us instead of using the form below. We will send you a payment link
you can use another way, and activate your licence once it clears.
"""

DEFAULT_BANK_DETAILS_MD = """\
_Bank details have not been configured yet._

Set them in the admin panel under Settings before enabling manual payments.
"""


async def get_settings(db: AsyncSession) -> AppSettings:
    """Fetch the settings row, creating it on first use.

    Seeded rather than assumed: a fresh database (a PR environment, a local
    checkout) would otherwise 500 on the first request to the paywall.
    """
    result = await db.execute(select(AppSettings).where(AppSettings.id == 1))
    row = result.scalar_one_or_none()
    if row is None:
        row = AppSettings(
            id=1,
            one_time_price_usd=env.PRICE_ONETIME_USD,
            currency="USD",
            bank_details_md=DEFAULT_BANK_DETAILS_MD,
            payment_instructions_md=DEFAULT_INSTRUCTIONS_MD,
            support_email="support@peptora.io",
            review_sla_hours=24,
            # Off until real bank details are entered — the placeholder above
            # is not something a customer should ever be shown.
            manual_payments_enabled=False,
            crypto_payments_enabled=False,
        )
        db.add(row)
        await db.flush()
        logger.info("app_settings seeded")
    return row
