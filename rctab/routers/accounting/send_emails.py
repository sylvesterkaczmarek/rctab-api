"""Tools for sending email notifications to users."""

import logging
from contextlib import AbstractAsyncContextManager
from datetime import date, datetime, timedelta, timezone
from itertools import groupby
from types import TracebackType
from typing import (
    Any,
    AsyncContextManager,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Type,
)
from uuid import UUID

from jinja2 import Environment, PackageLoader, exceptions
from rctab_models.models import SubscriptionState, SubscriptionStatus
from sendgrid import Mail, SendGridAPIClient
from sqlalchemy import and_, asc, desc, func, insert, or_, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.sql import Select

from rctab.constants import (
    EMAIL_TYPE_OVERBUDGET,
    EMAIL_TYPE_SUB_WELCOME,
    EMAIL_TYPE_SUMMARY,
    EMAIL_TYPE_TIMEBASED,
    EMAIL_TYPE_USAGE_ALERT,
)
from rctab.crud.accounting_models import (
    allocations,
    approvals,
    emails,
    failed_emails,
    finance,
    subscription,
    subscription_details,
)
from rctab.db import AsyncConnection
from rctab.routers.accounting.routes import (
    get_subscription_details,
    get_subscriptions,
    get_subscriptions_summary,
)
from rctab.settings import get_settings

# pylint: disable=too-many-arguments
# pylint: disable=unexpected-keyword-arg

logger = logging.getLogger(__name__)


async def _fetch_one(conn: AsyncConnection, query: Any) -> Optional[RowMapping]:
    """Execute a query and return the first row as a mapping."""
    result = await conn.execute(query)
    return result.mappings().first()


async def _fetch_all(conn: AsyncConnection, query: Any) -> Sequence[RowMapping]:
    """Execute a query and return all rows as mappings."""
    result = await conn.execute(query)
    return result.mappings().all()


async def _execute(conn: AsyncConnection, statement: Any) -> Any:
    """Execute an INSERT/UPDATE/DELETE statement and return a scalar if available."""
    result = await conn.execute(statement)
    return result.scalar_one_or_none() if result.returns_rows else None


async def get_sub_email_recipients(
    database: AsyncConnection, subscription_id: UUID
) -> List[str]:
    """Get the email adresses of users that should be emailed about this subscription.

    Args:
        database : The database recording the subscription and its users.
        subscription_id : The ID of the subscription to get user emails for.

    Returns:
        The email addresses of the users that should receive the email.
    """
    query = get_subscription_details(subscription_id)
    results = await _fetch_one(database, query)
    if results and results["role_assignments"]:
        assignments = results["role_assignments"]
        users_to_notify = [
            x
            for x in assignments
            if x["role_name"] in get_settings().notifiable_roles
            and str(subscription_id) in x["scope"]
        ]
        return [x["mail"] for x in users_to_notify if x.get("mail")]  # type: ignore
    return []


class MissingEmailParamsError(Exception):
    """Exception for when email settings are missing."""

    def __init__(
        self,
        subject: str,
        recipients: List[str],
        from_email: str,
        message: Optional[str] = None,
    ):
        """Capture the details of an email that couldn't be sent."""
        self.subject = subject
        self.recipients = recipients
        self.from_email = from_email
        self.message = message


def send_with_sendgrid(
    subject: str, template_name: str, template_data: Dict[str, Any], to_list: List[str]
) -> int:
    """Sends an email to those in to_list with subject=subject.

    Items in the jinja2 template are replaced with those in template_data.

    Args:
        subject : The subject of the email.
        template_name : The name of jinja2 template used to render the email.
        template_data : The data passed to the template.
        to_list : A string that contains the email addresses of the recipients.

    Returns:
        The status code that indicates whether or not email was sent sucessfully.

    Raises:
        MissingEmailParamsError: raises an error if the api key or the "from" email address is missing.
    """
    # pylint: disable=invalid-name
    try:
        rendered_template = render_template(template_name, template_data)
    except exceptions.UndefinedError:
        logger.exception("Error rendering html template.")
        return -999

    settings = get_settings()

    # We don't want to send any emails if we forget to mock the function
    assert not settings.testing

    api_key = settings.sendgrid_api_key
    from_email = settings.sendgrid_sender_email
    if not api_key or not from_email:
        raise MissingEmailParamsError(
            subject=subject,
            recipients=to_list,
            from_email=str(from_email),
            message=rendered_template,
        )

    message = Mail(
        from_email=from_email,
        to_emails=to_list,
        html_content=rendered_template,
        subject=subject,
    )

    logger.info("Sending an email to %s with subject=%s", to_list, subject)
    client = SendGridAPIClient(api_key)

    response = client.send(message)

    return response.status_code


async def send_generic_email(
    database: AsyncConnection,
    subscription_id: UUID,
    template_name: str,
    subject_prefix: str,
    email_type: str,
    template_data: Dict,
) -> None:
    """Sends template-based emails.

    Args:
        database: a Database, to keep a record of sent emails
        subscription_id: a subscription ID
        template_name: the name of a template in rctab/templates/emails/
        subject_prefix: prepended to subscription.name to make the email subject
        email_type: will be recorded in the database e.g. "subscription disabled"
        template_data: will be union-ed with the subscription summary data
                       and passed to the template
    """
    settings = get_settings()
    if not settings.ignore_whitelist:
        whitelist = settings.whitelist
        if subscription_id not in whitelist:
            logger.info(
                "Subscription %s is not in whitelist and will not be processed further.",
                subscription_id,
            )
            return

    sub_summary = await _fetch_one(
        database, get_subscriptions_summary(sub_id=subscription_id)
    )
    if sub_summary and sub_summary["abolished"]:
        logger.info(
            "Subscription %s is abolished and will not receive email notifications.",
            subscription_id,
        )
        return

    subscription_name = None
    if sub_summary:
        template_data["summary"] = dict(sub_summary)
        subscription_name = sub_summary["name"]

    if not subscription_name:
        # In case the status function app hasn't run yet
        subscription_name = f"{subscription_id}"

    recipients = await get_sub_email_recipients(database, subscription_id)
    if not recipients:
        logger.info(
            "No email recipients found for %s. Mailing RCTab admins instead.",
            subscription_id,
        )
        recipients = settings.admin_email_recipients
        subject_prefix = "RCTab undeliverable: " + subject_prefix
    template_data["rctab_url"] = settings.website_hostname
    try:
        status = send_with_sendgrid(
            subject_prefix + " " + subscription_name,
            template_name,
            template_data,
            recipients,
        )
        insert_statement = insert(emails).values(
            {
                "subscription_id": subscription_id,
                "status": status,
                "type": email_type,
                "recipients": ";".join(recipients),
                "extra_info": template_data.get("extra_info"),
            }
        )
        await _execute(database, insert_statement)
    except MissingEmailParamsError as error:
        insert_statement = (
            insert(failed_emails)
            .values(
                {
                    "subscription_id": subscription_id,
                    "type": email_type,
                    "subject": error.subject,
                    "recipients": ";".join(error.recipients),
                    "from_email": error.from_email,
                    "message": error.message,
                }
            )
            .returning(failed_emails.c.id)
        )
        row = await _execute(database, insert_statement)
        logger.error(
            "'%s' email failed to send to subscription '%s' due to missing "
            "api_key or send email address.\n"
            "It has been logged in the 'failed_emails' table with id=%s.\n"
            "Use 'get_failed_emails.py' to retrieve it to send manually.",
            email_type,
            subscription_id,
            row,
        )


async def send_expiry_looming_emails(
    database: AsyncConnection,
    subscription_expiry_dates: List[Tuple[UUID, date, SubscriptionState]],
) -> None:
    """Send an email to notify users that a subscription is nearing its expiry date.

    Args:
        database: A database to keep record of subscriptions and of sent emails.
        subscription_expiry_dates: A list of subscription ids and expiry dates for
            subscriptions nearing expiry.
    """
    # pylint: disable=use-dict-literal
    email_query = sub_time_based_emails()

    for (
        subscription_id,
        expiry_date,
        status,
    ) in subscription_expiry_dates:
        row = await _fetch_one(
            database, email_query.where(emails.c.subscription_id == subscription_id)
        )
        last_email_date = row["time_created"].date() if row else None

        if should_send_expiry_email(expiry_date, last_email_date, status):
            days_remaining = (expiry_date - date.today()).days
            await send_generic_email(
                database,
                subscription_id,
                "expiry_looming.html",
                f"{days_remaining} days until the expiry of your"
                + " Azure subscription:",
                EMAIL_TYPE_TIMEBASED,
                {"days": days_remaining, "extra_info": str(days_remaining)},
            )


async def send_overbudget_emails(
    database: AsyncConnection,
    overbudget_subs: List[Tuple[UUID, float]],
) -> None:
    """Send an email to notify users that subscription is overbudget.

    Args:
        database: The database recording subscriptions and sent emails.
        overbudget_subs: A list of subscription ids and the percentage of budget used for
            subscriptions that exceeded their budget.
    """
    # pylint: disable=use-dict-literal
    email_query = sub_usage_emails()

    for subscription_id, percentage_used in overbudget_subs:
        row = await _fetch_one(
            database, email_query.where(emails.c.subscription_id == subscription_id)
        )
        last_email_date = row["time_created"].date() if row else None

        if last_email_date is None or last_email_date < date.today():
            await send_generic_email(
                database,
                subscription_id,
                "usage_alert.html",
                str(percentage_used)
                + "% of allocated budget "
                + "used by your Azure subscription:",
                EMAIL_TYPE_OVERBUDGET,
                {
                    "percentage_used": percentage_used,
                    "extra_info": str(percentage_used),
                },
            )


def sub_time_based_emails() -> Select:
    """Builds a query to get the most recent time-based email for each subscription.

    Returns:
        SELECT statement for database query.
    """
    most_recent = (
        select(func.max_(emails.c.id).label("max_id"))
        .where(emails.c.type == EMAIL_TYPE_TIMEBASED)
        .group_by(emails.c.subscription_id)
        .alias()
    )

    return select(emails).select_from(
        emails.join(most_recent, emails.c.id == most_recent.c.max_id)
    )


def sub_usage_emails() -> Select:
    """Builds a query to get the most recent warning emails each subscription.

    Warning emails include over-budget, usage alert and expiry-looming emails.
    """
    most_recent = (
        select(func.max_(emails.c.id).label("max_id"))
        .where(
            or_(
                emails.c.type == EMAIL_TYPE_OVERBUDGET,
                emails.c.type == EMAIL_TYPE_TIMEBASED,
                emails.c.type == EMAIL_TYPE_USAGE_ALERT,
            )
        )
        .group_by(emails.c.subscription_id)
        .alias()
    )

    return select(emails).select_from(
        emails.join(most_recent, emails.c.id == most_recent.c.max_id)
    )


def should_send_expiry_email(
    date_of_expiry: date, date_of_last_email: Optional[date], status: SubscriptionState
) -> bool:
    """Work out whether we should send an email for this subscription.

    ~~~~|---------------------|-----|x==
       30                     7     1

    We will send an email on any of the | or - days, if there isn't already
    one in that period. This is to allow for emails not being sent (e.g. if
    the email service goes down). We don't send emails before this
    period (shown as ~) or on the day of expiry (denoted by x). We send daily
    emails to active subscriptions after expiry (marked with =). The exact
    date values of | are customisable via the settings module.

    Args:
        date_of_expiry: Expiry date of a subscription.
        date_of_last_email: Date of last expiry-looming email.
        status: Current status of the subscription.

    Returns:
        Whether to send an email.
    """
    # pylint: disable=simplifiable-if-statement,no-else-return
    if status not in (SubscriptionState.ENABLED, SubscriptionState.PASTDUE):
        return False

    if date_of_expiry < date.today():
        if status == SubscriptionState.ENABLED and (
            date_of_last_email is None or date_of_last_email < date.today()
        ):
            return True
        else:
            return False

    # Not technically a frequency, more of a schedule.
    expiry_email_schedule: List[int] = get_settings().expiry_email_freq
    for days_remaining in expiry_email_schedule:
        if date_of_expiry <= date.today() + timedelta(days=days_remaining):
            if not date_of_last_email:
                return True
            elif date_of_last_email < date_of_expiry - timedelta(days=days_remaining):
                # e.g. we are 6 days before expiry and the last email
                #      was more than 7 days before expiry
                return True

    return False


async def check_for_subs_nearing_expiry(database: AsyncConnection) -> None:
    """Check for subscriptions that should trigger an email as they near expiry.

    Args:
        database: Holds a record of the subscription, including its expiry date.
    """
    summary = get_subscriptions_summary().alias()

    # We don't _have_ to filter on approved_to, but it might make things slightly quicker
    expiry_query = (
        select(summary.c.subscription_id, summary.c.approved_to, summary.c.status)
        .where(
            and_(
                summary.c.abolished == False,  # pylint: disable=singleton-comparison
                summary.c.approved_to <= date.today() + timedelta(days=30),
            )
        )
        .order_by(summary.c.approved_to)
    )
    rows = await _fetch_all(database, expiry_query)

    within_thirty_days = [
        (row["subscription_id"], row["approved_to"], row["status"]) for row in rows
    ]
    if within_thirty_days:
        await send_expiry_looming_emails(database, within_thirty_days)


async def check_for_overbudget_subs(database: AsyncConnection) -> None:
    """Check for subscriptions that are overbudget and should trigger an email.

    Args:
        database: The database containing a record of the subscription, including budget information.
    """
    overbudget_subs = []

    summary = get_subscriptions_summary().alias()

    overbudget_query = (
        select(summary.c.subscription_id, summary.c.allocated, summary.c.total_cost)
        .where(
            and_(
                summary.c.abolished == False,  # pylint: disable=singleton-comparison
                summary.c.total_cost > summary.c.allocated,
            )
        )
        .where(
            or_(
                summary.c.status == SubscriptionState("Enabled"),
                summary.c.status == SubscriptionState("PastDue"),
            ),
        )
    )

    for row in await _fetch_all(database, overbudget_query):
        if (
            row["allocated"] is not None
            and row["allocated"] != 0
            and row["total_cost"] is not None
        ):
            percentage_used = round(row["total_cost"] / row["allocated"] * 100.0, 2)
        else:
            percentage_used = float("inf")

        overbudget_subs.append((row["subscription_id"], percentage_used))

    if overbudget_subs:
        await send_overbudget_emails(database, overbudget_subs)


class UsageEmailContextManager(AbstractAsyncContextManager):
    """Compare usage at enter and exit, sending emails as necessary."""

    def __init__(self, database: AsyncConnection):
        """Initialise the context manager."""
        self.database = database
        self.thresholds = (0.50, 0.75, 0.9, 0.95)

    async def __aenter__(self) -> AsyncContextManager:
        """Get a snapshot of usage."""
        # pylint: disable=singleton-comparison
        # pylint: disable=attribute-defined-outside-init

        logger.info("Getting usage snapshot")
        start = datetime.now()

        summary = get_subscriptions_summary().alias()
        summaries = await _fetch_all(self.database, select(summary))

        self.at_enter = []
        for lower in self.thresholds:
            # All the subscriptions that are already >= lower % of their budget
            self.at_enter.append(
                tuple(
                    x["subscription_id"]
                    for x in summaries
                    if x["total_cost"] and x["total_cost"] >= x["allocated"] * lower
                )
            )

        logger.info("Getting usage snapshot took %s", datetime.now() - start)

        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> bool:
        """Compare new usage with previous and send out emails."""
        # pylint: disable=singleton-comparison

        logger.info("Checking usage against thresholds")
        start = datetime.now()

        summary = get_subscriptions_summary().alias()
        summaries = await _fetch_all(self.database, select(summary))

        for i, lower in enumerate(self.thresholds):
            upper = (
                self.thresholds[i + 1] if len(self.thresholds) > i + 1 else float("inf")
            )

            # Subscriptions that are newly >= lower % and aren't "always on"
            exit_sub_ids = tuple(
                x["subscription_id"]
                for x in summaries
                if x["total_cost"]
                and x["total_cost"] >= lower * x["allocated"]
                and (x["total_cost"] < upper * x["allocated"] or x["allocated"] == 0.0)
                and x["subscription_id"] not in self.at_enter[i]
            )

            percentage_used = lower * 100.0
            for subscription_id in exit_sub_ids:
                await send_generic_email(
                    self.database,
                    subscription_id,
                    "usage_alert.html",
                    str(percentage_used)
                    + "% of allocated budget "
                    + "used by your Azure subscription:",
                    EMAIL_TYPE_USAGE_ALERT,
                    {
                        "percentage_used": percentage_used,
                        "extra_info": str(percentage_used),
                    },
                )

        logger.info("Checking usage against thresholds took %s", datetime.now() - start)

        return False


def prepare_welcome_email(
    database: AsyncConnection, new_status: SubscriptionStatus
) -> Dict:
    """Prepare arguments for sending a welcome email for a new_status, if necessary.

    Given a new and an old SubscriptionStatus, return a dictionary of arguments that can be given
    as `send_generic_email(**prepare_welcome_email(...))` to send a welcome email for
    the new status.
    """
    template_data = {}  # type: Dict
    return {
        "database": database,
        "subscription_id": new_status.subscription_id,
        "template_name": "welcome.html",
        "subject_prefix": "You have a new subscription on the Azure platform:",
        "email_type": EMAIL_TYPE_SUB_WELCOME,
        "template_data": template_data,
    }


def prepare_subscription_status_email(
    database: AsyncConnection,
    new_status: SubscriptionStatus,
    old_status: SubscriptionStatus,
) -> Dict:
    """Prepare arguments for sending a status change email, if necessary.

    Given a new and an old SubscriptionStatus, return a dictionary of arguments that can be given
    as `send_generic_email(**prepare_subscription_status_email(...))` to send a status
    change email. Unless the new and the old status are the same, and thus no email
    is needed, in which case return an empty dictionary.
    """
    status_has_changed = new_status.display_name != old_status.display_name
    if not status_has_changed:
        return {}
    template_data = {"new_status": new_status, "old_status": old_status}
    return {
        "database": database,
        "subscription_id": new_status.subscription_id,
        "template_name": "status_change.html",
        "subject_prefix": "There has been a status change for your Azure subscription:",
        "email_type": "subscription status",
        "template_data": template_data,
    }


def prepare_roles_email(
    database: AsyncConnection,
    new_status: SubscriptionStatus,
    old_status: SubscriptionStatus,
) -> Dict[Any, Any]:
    """Prepare arguments for sending a role assignment change email, if necessary.

    Given a new and an old SubscriptionStatus, return a dictionary of arguments that can be given
    as `send_generic_email(**prepare_roles_email(...))` to send a role assignment change
    email. Unless the new and the old role assignment are the same, and thus no email
    is needed, in which case return an empty dictionary.
    """
    # Convert to Dicts so we can remove items
    old_rbac = [x.model_dump() for x in old_status.role_assignments]
    new_rbac = [x.model_dump() for x in new_status.role_assignments]

    # We don't need to display these in emails
    for rbac_list in (old_rbac, new_rbac):
        for rbac in rbac_list:
            del rbac["principal_id"]
            del rbac["role_definition_id"]
            del rbac["scope"]

    removed_from_rbac = [x for x in old_rbac if x not in new_rbac]
    added_to_rbac = [x for x in new_rbac if x not in old_rbac]

    roles_have_changed = len(removed_from_rbac) + len(added_to_rbac) > 0
    if not roles_have_changed:
        return {}
    template_data = {
        "removed_from_rbac": removed_from_rbac,
        "added_to_rbac": added_to_rbac,
    }
    return {
        "database": database,
        "subscription_id": new_status.subscription_id,
        "template_name": "role_assignment_change.html",
        "subject_prefix": "The user roles have changed for your Azure subscription:",
        "email_type": "subscription roles",
        "template_data": template_data,
    }


async def send_status_change_emails(
    database: AsyncConnection,
    new_status: SubscriptionStatus,
    old_status: Optional[SubscriptionStatus] = None,
) -> None:
    """Send emails after a change in subscription status.

    The possible emails to send are a welcome email if the subscription is new, a status
    change email if its status has changed, and/or a role assignment email if the roles
    have changed.

    If old_status is None, this is interpreted as meaning that the subscription is new.
    """
    if old_status:
        status_kwargs = prepare_subscription_status_email(
            database, new_status, old_status
        )
        if status_kwargs:
            await send_generic_email(**status_kwargs)
        roles_kwargs = prepare_roles_email(database, new_status, old_status)
        if roles_kwargs:
            await send_generic_email(**roles_kwargs)
    else:
        # This shouldn't happen since we will have a status if we've
        # previously sent a welcome email
        logger.warning("Unreachable email code has been reached")


async def get_new_subscriptions_since(
    database: AsyncConnection, since_this_datetime: datetime
) -> Sequence[RowMapping]:
    """Returns a list of all the subscriptions created since the provided datetime."""
    subscription_query = select(subscription).where(
        subscription.c.time_created > since_this_datetime
    )
    rows = await _fetch_all(database, subscription_query)
    return rows


async def get_subscription_details_since(
    database: AsyncConnection, subscription_id: UUID, since_this_datetime: datetime
) -> Optional[Tuple[dict, dict]]:
    """Get the oldest and newest rows of the subscription details table.

    Filter by subscription id and time created.

    Args:
        database: A database.
        subscription_id: A subscription id.
        since_this_datetime: Get information from this datetime until now.

    Returns:
        The oldest and newest row of the subscription details table.
    """
    logger.info("Looking for subscription details since %s", since_this_datetime)
    status_query = (
        select(subscription_details)
        .where(subscription_details.c.subscription_id == subscription_id)
        .where(subscription_details.c.time_created > since_this_datetime)
        .order_by(asc(subscription_details.c.id))
    )
    rows = await _fetch_all(database, status_query)
    if not rows:
        return None
    return dict(rows[0]), dict(rows[-1])


async def get_emails_sent_since(
    database: AsyncConnection, since_this_datetime: datetime
) -> List[Dict]:
    """Get information about emails sent since a given time.

    Ignores summary emails.

    Args:
        database: a Database, with a record of sent emails
        since_this_datetime: datetime, get information from this datetime until now

    Returns:
        A list of dicts, each element represents a subscription for which at least one email
        was sent since the specified datetime.
    """
    emails_query = (
        select(emails)
        .where(emails.c.type != EMAIL_TYPE_SUMMARY)
        .where(emails.c.time_created > since_this_datetime)
    )
    rows = await _fetch_all(database, emails_query)
    all_emails_sent: List[dict] = [dict(row) for row in rows]

    emails_by_subscription = []
    for key, value in groupby(all_emails_sent, extract_sub_id):
        name_query = (
            select(subscription_details.c.display_name.label("name"))
            .where(subscription_details.c.subscription_id == key)
            .order_by(desc(subscription_details.c.time_created))
            .limit(1)
        )

        name_rows = await _fetch_all(database, name_query)
        if name_rows:
            name = name_rows[0]["name"]
            sub_dict = {"subscription_id": key, "name": name}
            list_emails = [
                {
                    "type": x["type"],
                    "time_created": x["time_created"],
                    "extra_info": x.get("extra_info", None),
                }
                for x in value
            ]
            sub_dict["emails_sent"] = list_emails
            emails_by_subscription.append(sub_dict)

    return emails_by_subscription


async def get_finance_entries_since(
    database: AsyncConnection, since_this_datetime: datetime
) -> List[dict]:
    """Get finance info grouped by subscription id.

    Args:
        database: The database to query.
        since_this_datetime: Only finance records created after this datetime.

    Returns:
        Each element of the list is a subscription with one or more finance
        items.
    """
    finance_query = select(finance).where(finance.c.time_created > since_this_datetime)
    rows = await _fetch_all(database, finance_query)
    all_new_entries = [dict(row) for row in rows]
    entries_by_subscription = []
    for key, value in groupby(all_new_entries, extract_sub_id):
        name_query = (
            select(subscription_details.c.display_name.label("name"))
            .where(subscription_details.c.subscription_id == key)
            .order_by(desc(subscription_details.c.time_created))
            .limit(1)
        )

        name_rows = await _fetch_all(database, name_query)
        if name_rows:
            name = name_rows[0]["name"]
            sub_dict = {"subscription_id": key, "name": name}
            list_entries = [
                {
                    "time_created": x["time_created"],
                    "amount": x["amount"],
                }
                for x in value
            ]
            sub_dict["finance_entry"] = list_entries
            entries_by_subscription.append(sub_dict)

    return entries_by_subscription


def extract_sub_id(my_dict: Mapping) -> UUID:
    """Returns the value for the key "subscription_id"."""
    return my_dict["subscription_id"]


async def prepare_summary_email(
    database: AsyncConnection, since_this_datetime: Optional[datetime] = None
) -> dict:
    """Prepares and returns data needed for the summary email.

    Args:
        database: a Database, with record of Azure subscriptions
        since_this_datetime: datetime, get information from this datetime until now

    Returns:
        A dictionary containing the data required for the summary email.
    """
    new_subscriptions_data = []
    status_changes = []
    new_approvals_and_allocations = []
    if not since_this_datetime:
        # the first time we send a summary email use information of last 24h
        since_this_datetime = datetime.now(timezone.utc) - timedelta(days=1)
    # get new subscriptions
    rows = await get_new_subscriptions_since(database, since_this_datetime)
    new_subscriptions = [row["subscription_id"] for row in rows]

    # get details of new subscriptions
    for sub_id in new_subscriptions:
        query = get_subscriptions_summary(sub_id)
        summary = await _fetch_one(database, query)
        if summary:
            new_subscriptions_data.append(dict(summary))

    # get info about status changes and approvals/allocations
    query = get_subscriptions()
    all_subscriptions = await _fetch_all(database, query)
    for sub in all_subscriptions:
        sub_details_since = await get_subscription_details_since(
            database, sub["subscription_id"], since_this_datetime
        )

        # Has there been a status change for this subscription
        # (new subscriptions do not count as status changes)?
        if sub_details_since and (
            sub_details_since[0]["state"] != sub_details_since[1]["state"]
            or sub_details_since[0]["display_name"]
            != sub_details_since[1]["display_name"]
        ):
            status_changes += [
                {
                    "old_status": sub_details_since[0],
                    "new_status": sub_details_since[1],
                }
            ]
        # get information about new allocations
        allocations_since = await get_allocations_since(
            database, sub["subscription_id"], since_this_datetime
        )
        approvals_since = await get_approvals_since(
            database, sub["subscription_id"], since_this_datetime
        )

        if allocations_since or approvals_since:
            query = get_subscription_details(sub["subscription_id"])
            sub_details = await _fetch_one(database, query)
            new_approvals_and_allocations += [
                {
                    "allocations": allocations_since,
                    "approvals": approvals_since,
                    "details": sub_details,
                }
            ]

    # get info about emails sent
    emails_sent = await get_emails_sent_since(database, since_this_datetime)
    num_emails = sum(len(item["emails_sent"]) for item in emails_sent)
    finance_entries = await get_finance_entries_since(database, since_this_datetime)
    num_finance = sum(len(item["finance_entry"]) for item in finance_entries)

    template_data = {
        "new_subscriptions": new_subscriptions_data,
        "status_changes": status_changes,
        "new_approvals_and_allocations": new_approvals_and_allocations,
        "notifications_sent": emails_sent,
        "num_notifications": num_emails,
        "finance": finance_entries,
        "num_finance": num_finance,
        "time_last_summary": since_this_datetime.replace(microsecond=0).replace(
            tzinfo=None
        ),
        "time_now": datetime.now(timezone.utc).replace(microsecond=0),
    }

    return template_data


async def get_allocations_since(
    database: AsyncConnection, subscription_id: UUID, since_this_datetime: datetime
) -> List:
    """Get new allocations since given datetime.

    Args:
        database: a Database, with record of allocations
        subscription_id: a UUID for the subscription
        since_this_datetime: datetime, get information from this datetime until now

    Returns:
        A list with all allocations for the given subscription id since the provided datetime.
    """
    query = (
        select(allocations.c.amount)
        .where(allocations.c.subscription_id == subscription_id)
        .where(allocations.c.time_created > since_this_datetime)
    )
    rows = await _fetch_all(database, query)
    allocations_since = [r["amount"] for r in rows]
    return allocations_since


async def get_approvals_since(
    database: AsyncConnection, subscription_id: UUID, since_this_datetime: datetime
) -> List:
    """Get new approvals since given datetime.

    Args:
        database: a Database, with record of approvals
        subscription_id: a UUID for the subscription
        since_this_datetime: datetime, get information from this datetime until now

    Returns:
        A list with all approvals for the given subscription id since the provided datetime.
    """
    query = (
        select(approvals.c.amount)
        .where(approvals.c.subscription_id == subscription_id)
        .where(approvals.c.time_created > since_this_datetime)
    )
    rows = await _fetch_all(database, query)
    approvals_since = [r["amount"] for r in rows]
    return approvals_since


def render_template(template_name: str, template_data: Dict[str, Any]) -> str:
    """Renders html based on the provided template and data.

    Args:
        template_name : The name of template.
        template_data : The data used to render the template.

    Returns:
        The rendered template as a string.
    """
    env = Environment(loader=PackageLoader("rctab", "templates/emails"))
    template = env.get_template(template_name)
    rendered_template = template.render(**template_data)
    return rendered_template
