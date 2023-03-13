"""Services for the job_script_templates resource, including module specific business logic."""
import dataclasses

from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from jobbergate_api.apps.job_script_templates.models import JobScriptTemplate
from jobbergate_api.apps.job_script_templates.schemas import (
    JobTemplateCreateRequest,
    JobTemplateUpdateRequest,
)


@dataclasses.dataclass
class JobScriptTemplateService:

    session: AsyncSession

    async def create(self, incoming_data: JobTemplateCreateRequest, owner_email: str) -> JobScriptTemplate:
        """Add a new job_script_template to the database."""

        job_script_template = JobScriptTemplate(
            **incoming_data.dict(exclude_unset=True),
            owner_email=owner_email,
        )
        self.session.add(job_script_template)
        await self.session.flush()
        await self.session.refresh(job_script_template)
        return job_script_template

    async def count(self) -> int:
        """Count the number of job_script_templates on the database."""
        result = await self.session.execute(select(func.count(JobScriptTemplate.id)))
        return result.scalar_one()

    async def get(self, id_or_identifier: int | str) -> JobScriptTemplate | None:
        """Get a job_script_template by id or identifier."""
        query = select(JobScriptTemplate)
        query = _locate_by_id_or_identifier(id_or_identifier, query)
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def delete(self, id_or_identifier: int | str) -> None:
        """Delete a job_script_template by id or identifier."""
        job_template = await self.get(id_or_identifier)
        if job_template is None:
            return
        await self.session.delete(job_template)
        await self.session.flush()

    async def update(
        self, id_or_identifier: int | str, incoming_data: JobTemplateUpdateRequest
    ) -> JobScriptTemplate:
        """Update a job_script_template by id or identifier."""
        query = update(JobScriptTemplate).returning(JobScriptTemplate)
        query = _locate_by_id_or_identifier(id_or_identifier, query)
        query = query.values(**incoming_data.dict(exclude_unset=True))
        result = await self.session.execute(query)
        await self.session.flush()
        return result.scalar_one()


def list():
    pass


def _locate_by_id_or_identifier(id_or_identifier: int | str, query):
    if isinstance(id_or_identifier, str):
        query = query.where(JobScriptTemplate.identifier == id_or_identifier)
    elif isinstance(id_or_identifier, int):
        query = query.where(JobScriptTemplate.id == id_or_identifier)
    else:
        raise ValueError("id_or_identifier must be a string or integer")
    return query
