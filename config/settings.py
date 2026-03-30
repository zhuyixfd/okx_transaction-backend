
"""
    This file includes all the configuration file for API
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Handles documentation title
app = FastAPI(
    version="1.0.0",
    title="okx跟单系统",
    description="",
    docs_url="/", redoc_url= None
)

# Handles cors
origins = []

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
