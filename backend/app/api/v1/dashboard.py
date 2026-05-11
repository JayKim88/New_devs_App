from decimal import Decimal, ROUND_HALF_UP
from fastapi import APIRouter, Depends, HTTPException
from typing import Dict, Any
from app.services.cache import get_revenue_summary
from app.core.auth import authenticate_request as get_current_user

router = APIRouter()

@router.get("/dashboard/summary")
async def get_dashboard_summary(
    property_id: str,
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:

    tenant_id = getattr(current_user, "tenant_id", "default_tenant") or "default_tenant"

    revenue_data = await get_revenue_summary(property_id, tenant_id)

    # PRECISION: revenue_data['total'] is the string form of a Decimal SUM from
    # the DB (e.g. "2250.000"). Going straight through float() loses precision
    # for fractional values (IEEE 754) — the source of finance's "off by a few
    # cents" reports. Keep precision in Decimal, round to USD cents at the API
    # boundary, then convert.
    total_decimal = Decimal(revenue_data['total'])
    total_cents = total_decimal.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    return {
        "property_id": revenue_data['property_id'],
        "total_revenue": float(total_cents),
        "currency": revenue_data['currency'],
        "reservations_count": revenue_data['count']
    }
