"""Set and get usage data."""

import datetime
import logging
from typing import Dict, List
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel
from rctab_models.models import AllCMUsage, AllUsage, CMUsage, Usage, UserRBAC
from sqlalchemy import delete, select

# require the postgres specific insert rather than the generic sqlachemy for
# `post_cm_usage` fn where "excluded" and "on_conflict_do_update" are used.
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.inspection import inspect

from rctab.constants import ADMIN_OID
from rctab.crud import accounting_models
from rctab.crud.accounting_models import refresh_materialised_view, usage_view
from rctab.crud.auth import token_admin_verified
from rctab.crud.utils import insert_subscriptions_if_not_exists
from rctab.db import AsyncConnection, get_async_connection
from rctab.routers.accounting.desired_states import refresh_desired_states
from rctab.routers.accounting.routes import router
from rctab.routers.accounting.send_emails import UsageEmailContextManager
from rctab.settings import get_settings

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

logger = logging.getLogger(__name__)


class TmpReturnStatus(BaseModel):
    """A wrapper for a status message."""

    status: str


async def authenticate_usage_app(token: str = Depends(oauth2_scheme)) -> Dict[str, str]:
    """Authenticates the usage function app."""
    headers = {"WWW-Authenticate": "Bearer"}

    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers=headers,
    )
    missing_key_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials - endpoint doesn't have public key",
        headers=headers,
    )

    public_key = get_settings().usage_func_public_key

    if not public_key:

        raise missing_key_exception

    try:
        payload = jwt.decode(
            token, public_key, algorithms=["RS256"], options={"require": ["exp", "sub"]}
        )
        username = payload.get("sub")

        if username != "usage-app":
            raise credentials_exception
    except HTTPException as e:
        logger.error(e)
        raise credentials_exception
    return payload


async def insert_usage(conn: AsyncConnection, all_usage: AllUsage) -> None:
    """Inserts usage into the database.

    Args:
        conn: Database connection to execute SQL statements.
        all_usage: Usage data to insert.
    """
    usage_query = insert(accounting_models.usage)

    logger.info("Inserting usage data")
    insert_start = datetime.datetime.now()

    usage_rows = [i.model_dump() for i in all_usage.usage_list]
    if usage_rows:
        await conn.execute(usage_query, usage_rows)
    logger.info("Inserting usage data took %s", datetime.datetime.now() - insert_start)
    refresh_start = datetime.datetime.now()
    await refresh_materialised_view(conn, usage_view)
    logger.info(
        "Refreshing the usage view took %s",
        datetime.datetime.now() - refresh_start,
    )


async def delete_usage(
    conn: AsyncConnection, start_date: datetime.date, end_date: datetime.date
) -> None:
    """Deletes usage(s) within a date range from the database.

    Args:
        conn: Database connection to execute SQL statements.
        start_date: Defines the beginning of the date range.
        end_date: Defines the end of the date range.
    """
    usage_query = (
        delete(accounting_models.usage)
        .where(accounting_models.usage.c.date >= start_date)
        .where(accounting_models.usage.c.date <= end_date)
    )

    logger.info("Delete usage data within a date range")
    delete_start = datetime.datetime.now()

    await conn.execute(usage_query)

    logger.info("Delete usage data took %s", datetime.datetime.now() - delete_start)


@router.post("/monthly-usage", response_model=TmpReturnStatus)
async def post_monthly_usage(
    all_usage: AllUsage,
    _: Dict[str, str] = Depends(authenticate_usage_app),
    conn: AsyncConnection = Depends(get_async_connection),
) -> TmpReturnStatus:
    """Inserts monthly usage data into the database."""
    logger.info("Post monthly usage called")

    if len(all_usage.usage_list) == 0:
        raise HTTPException(
            status_code=400,
            detail="Monthly usage data must have at least one record.",
        )

    post_start = datetime.datetime.now()

    for usage in all_usage.usage_list:
        if usage.monthly_upload is None:
            raise HTTPException(
                status_code=400,
                detail="Post monthly usage data must have the monthly_upload column populated.",
            )

    dates = sorted([x.date for x in all_usage.usage_list])
    date_min = dates[0]
    date_max = dates[-1]

    logger.info(
        "Post monthly usage received data for %s - %s containing %d records",
        date_min,
        date_max,
        len(all_usage.usage_list),
    )

    async with conn.begin_nested():

        logger.info(
            "Post monthly usage deleting existing usage data for %s - %s",
            date_min,
            date_max,
        )

        # Delete all usage for the time period to have a blank slate.
        query_del = (
            accounting_models.usage.delete()
            .where(accounting_models.usage.c.date >= date_min)
            .where(accounting_models.usage.c.date <= date_max)
        )
        await conn.execute(query_del)

        logger.info(
            "Post monthly usage inserting new subscriptions if they don't exist"
        )

        unique_subscriptions = list({i.subscription_id for i in all_usage.usage_list})

        await insert_subscriptions_if_not_exists(unique_subscriptions, conn)

        logger.info("Post monthly usage inserting monthly usage data")

        await insert_usage(conn, all_usage)

    # Note that we don't refresh the desired states here as we don't
    # want to trigger excess emails.

    logger.info("Post monthly usage data took %s", datetime.datetime.now() - post_start)

    return TmpReturnStatus(
        status=f"successfully uploaded {len(all_usage.usage_list)} rows"
    )


@router.post("/all-usage", response_model=TmpReturnStatus)
async def post_usage(
    all_usage: AllUsage,
    _: Dict[str, str] = Depends(authenticate_usage_app),
    conn: AsyncConnection = Depends(get_async_connection),
) -> TmpReturnStatus:
    """Write some usage data to the database."""
    post_start = datetime.datetime.now()

    async with UsageEmailContextManager(conn):

        async with conn.begin_nested():
            unique_subscriptions = list(
                {i.subscription_id for i in all_usage.usage_list}
            )
            await delete_usage(conn, all_usage.start_date, all_usage.end_date)

            await insert_subscriptions_if_not_exists(unique_subscriptions, conn)

            await insert_usage(conn, all_usage)

    await refresh_desired_states(conn, UUID(ADMIN_OID), unique_subscriptions)

    logger.info("POSTing usage took %s", datetime.datetime.now() - post_start)

    return TmpReturnStatus(
        status=f"successfully uploaded {len(all_usage.usage_list)} rows"
    )


async def get_usage(
    _: UserRBAC = Depends(token_admin_verified),
    conn: AsyncConnection = Depends(get_async_connection),
) -> List[Usage]:
    """Get all usage data."""
    usage_query = select(accounting_models.usage)
    return [Usage(**x) for x in (await conn.execute(usage_query)).mappings()]


@router.post("/all-cm-usage", response_model=TmpReturnStatus)
async def post_cm_usage(
    all_cm_usage: AllCMUsage,
    _: Dict[str, str] = Depends(authenticate_usage_app),
    conn: AsyncConnection = Depends(get_async_connection),
) -> TmpReturnStatus:
    """Write cost-management data to the database."""
    async with conn.begin_nested():
        unique_subscriptions = list(
            {i.subscription_id for i in all_cm_usage.cm_usage_list}
        )

        await insert_subscriptions_if_not_exists(unique_subscriptions, conn)

        cm_query = insert(accounting_models.costmanagement)
        update_dict = {c.name: c for c in cm_query.excluded if not c.primary_key}
        on_duplicate_key_stmt = cm_query.on_conflict_do_update(
            index_elements=inspect(accounting_models.costmanagement).primary_key,
            set_=update_dict,
        )

        await conn.execute(
            on_duplicate_key_stmt,
            [i.model_dump() for i in all_cm_usage.cm_usage_list],
        )

    return TmpReturnStatus(
        status=f"successfully uploaded {len(all_cm_usage.cm_usage_list)} rows"
    )


@router.get("/all-cm-usage", response_model=List[CMUsage])
async def get_cm_usage(
    _: UserRBAC = Depends(token_admin_verified),
    conn: AsyncConnection = Depends(get_async_connection),
) -> List[CMUsage]:
    """Get all cost-management data."""
    cm_query = select(accounting_models.costmanagement)
    return [CMUsage(**x) for x in (await conn.execute(cm_query)).mappings()]
