from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pytest_mock import MockerFixture

from rctab.routers.accounting import send_emails


@pytest.mark.asyncio
async def test_send_generic_email_skips_abolished_subscription(
    mocker: MockerFixture,
) -> None:
    """Abolished subscriptions must not receive any email notification."""
    database = AsyncMock()
    subscription_id = uuid4()

    settings = mocker.patch("rctab.routers.accounting.send_emails.get_settings")
    settings.return_value.ignore_whitelist = True

    fetch_one = mocker.patch(
        "rctab.routers.accounting.send_emails._fetch_one",
        new_callable=AsyncMock,
    )
    fetch_one.return_value = {"abolished": True, "name": "abolished subscription"}

    get_recipients = mocker.patch(
        "rctab.routers.accounting.send_emails.get_sub_email_recipients",
        new_callable=AsyncMock,
    )
    send_with_sendgrid = mocker.patch(
        "rctab.routers.accounting.send_emails.send_with_sendgrid"
    )

    await send_emails.send_generic_email(
        database,
        subscription_id,
        "blank.html",
        "Subject:",
        "test",
        {},
    )

    fetch_one.assert_awaited_once()
    get_recipients.assert_not_awaited()
    send_with_sendgrid.assert_not_called()
