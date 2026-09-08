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
### How to pay

1. Transfer the exact amount shown above to the account listed.
2. Save the receipt or take a screenshot of the confirmation.
3. Come back here, fill in the reference number and upload the receipt.

We check every payment by hand, so access is not instant. You will get an
email the moment your licence is active.

Prefer not to transfer directly? Email us and we will send you a payment
link instead.
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
