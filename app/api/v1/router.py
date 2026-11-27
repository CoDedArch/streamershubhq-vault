from fastapi import APIRouter

from app.api.v1.endpoints import (agreements, applications, channels, contact, moments, auth, capital, command, designs, desks, files, finance, hubby, network, people,
                                  posts, expenses, public, review, settings, store, store_admin, streamplan, suggestions, vault)

api_router = APIRouter()
for module in (auth, vault, public, applications, review, designs, store, store_admin, network, finance, command, people,
               desks, capital, settings, files, agreements, moments, posts, expenses, contact, suggestions, streamplan, channels, hubby):
    api_router.include_router(module.router)
