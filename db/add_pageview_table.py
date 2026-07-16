"""One-off script: creates any tables missing from the DB (currently just
`page_views`) without touching existing tables or data.

Safe to re-run any time new models are added to db/models.py - unlike
DataLoader.create_tables(), this never drops anything.

Usage: python -m db.add_pageview_table
"""
from db.database import Base, engine
from db import models  # noqa: F401 - import registers all models on Base.metadata

if __name__ == "__main__":
    Base.metadata.create_all(bind=engine)
    print("Done. Existing tables were left untouched; any missing tables were created.")
