"""Portal routes — self-service for logged-in users."""
from fastapi import APIRouter

from .me import router as me_router
from .keys import router as keys_router
from .teams import router as teams_router
from .usage import router as usage_router

router = APIRouter()
router.include_router(me_router)
router.include_router(keys_router)
router.include_router(teams_router)
router.include_router(usage_router)
