"""Links to the separately built demo apps. Placeholders only: unset => configured=false, never a made-up URL."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.auth.deps import OrgContext, get_container, org_context
from app.container import Container
from app.envelope import wrap

router = APIRouter(tags=["demo"])


def _link(url: str | None) -> dict:
    return {"url": url, "configured": bool(url)}


@router.get("/integrations/demo")
async def demo_links(request: Request, _: OrgContext = Depends(org_context), c: Container = Depends(get_container)):
    s = c.settings
    return wrap(request, {
        "pulselab": _link(s.pulselab_url),
        "shopflow_web": _link(s.shopflow_web_url),
        "shopflow_api": _link(s.shopflow_api_url),
        "shopflow_repository": s.shopflow_repository,
        "note": "Demo apps are built separately. TRACE reads ShopFlow only through its Vercel and GitHub connections.",
    })
