"""Services for the job_script_templates resource, including module specific business logic."""
import dataclasses
from typing import Any

from fastapi import UploadFile
from fastapi_pagination import Page
from fastapi_pagination.ext.sqlalchemy import paginate
from jinja2 import Template
from sqlalchemy import func, not_, select, update
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession

from jobbergate_api.apps.constants import FileType
from jobbergate_api.apps.job_script_templates.models import (
    JobScriptTemplate,
    JobScriptTemplateFile,
    WorkflowFile,
)
from jobbergate_api.apps.job_script_templates.schemas import (
    JobTemplateCreateRequest,
    JobTemplateUpdateRequest,
)
from jobbergate_api.storage import search_clause, sort_clause


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
            raise NoResultFound("JobScriptTemplate not found")
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

    async def list(
        self,
        all: bool = True,
        sort_ascending: bool = True,
        user_email: str | None = None,
        search: str | None = None,
        sort_field: str | None = None,
    ) -> Page[JobScriptTemplate]:
        """List all job_script_templates."""
        query = select(JobScriptTemplate)
        if user_email:
            query = query.where(JobScriptTemplate.owner_email == user_email)
        if not all:
            query = query.where(not_(JobScriptTemplate.identifier.is_(None)))
        if search:
            query = query.where(search_clause(search, JobScriptTemplate.searchable_fields))
        if sort_field:
            query = query.order_by(sort_clause(sort_field, JobScriptTemplate.sortable_fields, sort_ascending))

        return await paginate(self.session, query)


def _locate_by_id_or_identifier(id_or_identifier: int | str, query):
    if isinstance(id_or_identifier, str):
        query = query.where(JobScriptTemplate.identifier == id_or_identifier)
    elif isinstance(id_or_identifier, int):
        query = query.where(JobScriptTemplate.id == id_or_identifier)
    else:
        raise ValueError("id_or_identifier must be a string or integer")
    return query


@dataclasses.dataclass
class JobScriptTemplateFilesService:
    session: AsyncSession
    bucket: Any

    async def get(self, template_file: JobScriptTemplateFile):
        """Get a job_script_template file."""
        fileobj = await self.bucket.meta.client.get_object(
            Bucket=self.bucket.name, Key=template_file.file_key
        )
        file_content = await fileobj["Body"].read()
        yield file_content

    async def upsert(
        self,
        job_script_template_id: int,
        file_type: FileType,
        upload_file: UploadFile,
    ) -> JobScriptTemplateFile:
        """Upsert a job_script_template file."""
        template_file = JobScriptTemplateFile(
            id=job_script_template_id, filename=upload_file.filename, file_type=file_type
        )

        await self.bucket.upload_fileobj(Fileobj=upload_file.file, Key=template_file.file_key)

        merged = await self.session.merge(template_file)
        await self.session.flush()
        return merged

    async def delete(self, template_file: JobScriptTemplateFile) -> None:
        """Delete a job_script_template file."""
        await self.session.delete(template_file)
        await self.bucket.meta.client.delete_object(Bucket=self.bucket.name, Key=template_file.file_key)
        await self.session.flush()

    async def render(self, template_file: JobScriptTemplateFile, parameters: dict[str, Any]) -> str:
        """Render a job script template."""
        async for file_content in self.get(template_file):
            return str(Template(file_content.decode("utf-8")).render(**parameters))


@dataclasses.dataclass
class WorkflowFilesService:
    """Service for the workflow resource."""

    session: AsyncSession
    bucket: Any

    async def get(self, workflow_file: WorkflowFile):
        """Get a workflow file."""
        fileobj = await self.bucket.meta.client.get_object(
            Bucket=self.bucket.name, Key=workflow_file.file_key
        )
        file_content = await fileobj["Body"].read()
        yield file_content

    async def upsert(
        self,
        job_script_template_id: int,
        runtime_config: dict[str, Any],
        upload_file: UploadFile,
    ) -> WorkflowFile:
        """Upsert a workflow file."""
        workflow_file = WorkflowFile(id=job_script_template_id, runtime_config=runtime_config)

        await self.bucket.upload_fileobj(Fileobj=upload_file.file, Key=workflow_file.file_key)

        merged = await self.session.merge(workflow_file)
        await self.session.flush()
        return merged

    async def delete(self, workflow_file: WorkflowFile) -> None:
        """Delete a workflow file."""
        await self.session.delete(workflow_file)
        await self.bucket.meta.client.delete_object(Bucket=self.bucket.name, Key=workflow_file.file_key)
        await self.session.flush()