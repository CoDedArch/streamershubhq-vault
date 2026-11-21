from fastapi import APIRouter

from app.api.v1.endpoints import (agreements, applications, contact, moments, auth, capital, command, designs, desks, files, finance, network, people,
                                  posts, expenses, public, review, settings, store, store_admin, suggestions, vault)

api_router = APIRouter()
for module in (auth, vault, public, applications, review, designs, store, store_admin, network, finance, command, people,
               desks, capital, settings, files, agreements, moments, posts, expenses, contact, suggestions):
    api_router.include_router(module.router)
