"""Read-only forward prediction evidence endpoint."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.services.shadow_evidence import shadow_evidence_report

router = APIRouter(prefix="/crypto", tags=["shadow evidence"])


@router.get("/bindings/{binding_id}/evidence")
def evidence(binding_id: int, db: Session = Depends(get_db)):
    try:
        return shadow_evidence_report(db, binding_id)
    except ValueError:
        raise HTTPException(404, "Unknown shadow binding") from None
