
"""
    This file includes all the configuration file for API
"""
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Handles documentation title
app = FastAPI(
    version="1.0.0",
    title="okx跟单系统",
    description="",
    docs_url="/docs", redoc_url=None
)

# Handles cors
cors_origins = os.getenv("CORS_ORIGINS")
if cors_origins:
    origins = [o.strip() for o in cors_origins.split(",") if o.strip()]
else:
    # Sensible defaults for local dev (Vue Vite defaults).
    origins = ["http://localhost:5173", "http://127.0.0.1:5173"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
