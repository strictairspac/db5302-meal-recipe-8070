from fastapi import APIRouter, Depends, Response
from sqlalchemy import text

from src.config.settings import cache, engine
from src.controllers.workflow import download
from src.models.tables import audit, cases, evidence, runs
from src.schemas.workflow import AckForm, CaseForm, ClaimForm, LoginForm, ReviewForm
from src.services import claims, domain, workflow
from src.services.security import buyer, current_user, login, operator, reviewer

router = APIRouter()


@router.get('/health')
def health():
    with engine.connect() as conn:
        conn.execute(text('SELECT 1')).scalar_one()
    cache.ping()
    return {'ok': True}


@router.post('/login')
def sign_in(form: LoginForm):
    return login(form.username, form.password)


@router.get('/spec')
def spec(_=Depends(current_user)):
    return domain.SPEC


@router.get('/cases')
def list_cases(_=Depends(current_user)):
    return workflow.recent(cases)


@router.post('/cases', status_code=201)
def create_case(form: CaseForm, actor=Depends(operator)):
    return workflow.make_case(form.title, form.payload, actor)


@router.get('/runs')
def list_runs(_=Depends(current_user)):
    return workflow.recent(runs)


@router.post('/cases/{case_id}/runs', status_code=202)
def start_run(case_id: int, actor=Depends(operator)):
    return workflow.enqueue(case_id, actor)


@router.get('/evidence')
def list_evidence(_=Depends(current_user)):
    return workflow.recent(evidence)


@router.get('/evidence/{identity}/download')
def export_evidence(identity: int, _=Depends(current_user)):
    return download(identity)


@router.post('/evidence/{identity}/review')
def review_evidence(identity: int, form: ReviewForm, actor=Depends(reviewer)):
    return workflow.review(identity, form.note, actor)


@router.get('/audit')
def list_audit(_=Depends(current_user)):
    return workflow.recent(audit)


@router.post('/claims', status_code=201)
def claim_batch(form: ClaimForm, actor=Depends(buyer)):
    """Claim the next reviewed batch.  Returns 204 when nothing is pending."""
    result = claims.claim(form.terminal, form.token, actor)
    if result is None:
        return Response(status_code=204)
    return result


@router.get('/claims')
def list_claims(_=Depends(buyer)):
    return claims.recent()


@router.get('/claims/{identity}')
def get_claim(identity: int, _=Depends(buyer)):
    return claims.detail(identity)


@router.post('/claims/{identity}/ack')
def acknowledge_claim(identity: int, form: AckForm, actor=Depends(buyer)):
    return claims.acknowledge(identity, form.token, actor)
