import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from config import settings
from db import get_db
from services.event_intake import MalformedEventError, process_envelope, verify_signature

router = APIRouter(tags=["webhooks"])


@router.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request, db: Session = Depends(get_db)) -> dict:
    body = await request.body()
    signature_verified = False
    if settings.razorpay_webhook_secret:
        signature = request.headers.get("x-razorpay-signature", "")
        if not verify_signature(body, signature, settings.razorpay_webhook_secret):
            raise HTTPException(status_code=401, detail="invalid webhook signature")
        signature_verified = True
    try:
        envelope = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="body is not valid JSON")
    try:
        result = process_envelope(db, envelope, "RAZORPAY_WEBHOOK")
    except MalformedEventError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    result["signature_verified"] = signature_verified
    return result
