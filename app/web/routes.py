"""HTML routes aggregator — portal, platform, and public subpackages."""
from fastapi import APIRouter

from .public.auth import router as public_router
from .portal import router as portal_router
from .platform import router as platform_router

router = APIRouter()
router.include_router(public_router)
router.include_router(portal_router)
router.include_router(platform_router)
