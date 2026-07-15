"""Instance API. Internal-only during P2 (no auth yet — the service binds to the
control-plane VM's LAN interface; platform auth lands in P3 along with the
dashboard). The X-User-Id header stands in for the authenticated user until then."""

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import states
from app.config import get_settings
from app.database import get_db
from app.jobs import queue
from app.models import Event, Image, Instance, Plan, User

router = APIRouter(prefix="/instances", tags=["instances"])

LABEL_RE = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"  # DNS label — becomes <label>.BASE_DOMAIN


def current_user(db: Session = Depends(get_db), x_user_id: int = Header(...)) -> User:
    user = db.get(User, x_user_id)
    if user is None or user.status != "active":
        raise HTTPException(401, "unknown or suspended user")
    return user


class InstanceCreate(BaseModel):
    label: str = Field(pattern=LABEL_RE, max_length=63)
    plan: str
    image: str


class InstanceOut(BaseModel):
    id: int
    label: str
    hostname: str | None
    status: str
    ipv4: str | None
    plan: str
    image: str

    @classmethod
    def from_row(cls, i: Instance) -> "InstanceOut":
        return cls(
            id=i.id,
            label=i.label,
            hostname=i.hostname,
            status=i.status,
            ipv4=i.ip.address if i.ip else None,
            plan=i.plan.slug,
            image=i.image.slug,
        )


def _check_quota(db: Session, user: User, plan: Plan) -> None:
    live = db.scalars(
        select(Instance).where(Instance.user_id == user.id, Instance.status.notin_(states.TERMINAL_STATES))
    ).all()
    if len(live) >= user.max_instances:
        raise HTTPException(422, f"instance quota reached ({user.max_instances})")
    used_vcpus = sum(i.plan.vcpus for i in live)
    used_disk = sum(i.plan.disk_gb for i in live)
    if used_vcpus + plan.vcpus > user.max_vcpus or used_disk + plan.disk_gb > user.max_disk_gb:
        raise HTTPException(422, "vcpu/disk quota exceeded")


@router.post("", status_code=202, response_model=InstanceOut)
def create_instance(body: InstanceCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    plan = db.scalar(select(Plan).where(Plan.slug == body.plan, Plan.is_active.is_(True)))
    image = db.scalar(select(Image).where(Image.slug == body.image, Image.status == "active"))
    if plan is None or image is None:
        raise HTTPException(422, "unknown plan or image")
    _check_quota(db, user, plan)
    hostname = f"{body.label}.{get_settings().BASE_DOMAIN}"
    taken = db.scalar(
        select(func.count())
        .select_from(Instance)
        .where(Instance.hostname == hostname, Instance.status.notin_(states.TERMINAL_STATES))
    )
    if taken:
        raise HTTPException(409, f"{hostname} is taken")
    instance = Instance(user_id=user.id, plan_id=plan.id, image_id=image.id, label=body.label)
    db.add(instance)
    db.flush()
    db.add(
        Event(
            user_id=user.id,
            instance_id=instance.id,
            action="instance.create",
            status="started",
            detail={"plan": plan.slug, "image": image.slug},
        )
    )
    queue.enqueue(db, "instance.create", instance_id=instance.id)
    db.commit()
    return InstanceOut.from_row(instance)


@router.get("", response_model=list[InstanceOut])
def list_instances(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(Instance).where(Instance.user_id == user.id, Instance.status.notin_(states.TERMINAL_STATES))
    ).all()
    return [InstanceOut.from_row(i) for i in rows]


@router.get("/{instance_id}", response_model=InstanceOut)
def get_instance(instance_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    instance = db.get(Instance, instance_id)
    if instance is None or instance.user_id != user.id:
        raise HTTPException(404, "not found")
    return InstanceOut.from_row(instance)


ACTIONS = {
    "start": "instance.start",
    "stop": "instance.stop",
    "reboot": "instance.reboot",
    "rebuild": "instance.rebuild",
    "resize": "instance.resize",
    "destroy": "instance.destroy",
}


@router.post("/{instance_id}/actions/{action}", status_code=202)
def instance_action(instance_id: int, action: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    verb = ACTIONS.get(action)
    if verb is None:
        raise HTTPException(404, f"unknown action {action}")
    instance = db.get(Instance, instance_id)
    if instance is None or instance.user_id != user.id:
        raise HTTPException(404, "not found")
    if not states.can_enqueue(verb, instance.status):
        raise HTTPException(409, f"cannot {action} an instance in state {instance.status}")
    try:
        job = queue.enqueue(db, verb, instance_id=instance.id)
    except queue.JobConflict as e:
        raise HTTPException(409, str(e)) from e
    db.add(Event(user_id=user.id, instance_id=instance.id, action=verb, status="started", detail={}))
    db.commit()
    return {"job_id": job.id, "status": "queued"}
