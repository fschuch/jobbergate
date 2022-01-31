"""
Database model for the Application resource.
"""
from sqlalchemy import Boolean, DateTime, Integer, String, Table
from sqlalchemy.sql import False_, func
from sqlalchemy.sql.schema import Column

from jobbergate_api.metadata import metadata

applications_table = Table(
    "applications",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("application_name", String, nullable=False, index=True),
    Column("application_identifier", String, unique=True, index=True),
    Column("application_description", String, default=""),
    Column("application_owner_email", String, nullable=False, index=True),
    Column("application_file", String, nullable=False),
    Column("application_config", String, nullable=False),
    Column("application_uploaded", Boolean, nullable=False, default=False_()),
    Column("created_at", DateTime, nullable=False, default=func.now()),
    Column("updated_at", DateTime, nullable=False, default=func.now(), onupdate=func.now()),
)