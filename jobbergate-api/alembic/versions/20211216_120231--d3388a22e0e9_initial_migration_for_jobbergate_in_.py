"""Initial migration for Jobbergate in Armada

Revision ID: d3388a22e0e9
Revises: 
Create Date: 2021-12-16 12:02:31.989922

"""
import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "d3388a22e0e9"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "applications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("identifier", sa.String(), nullable=True),
        sa.Column("application_name", sa.String(), nullable=False),
        sa.Column("application_identifier", sa.String(), nullable=True),
        sa.Column("application_description", sa.String(), nullable=True),
        sa.Column("application_owner_email", sa.String(), nullable=False),
        sa.Column("application_file", sa.String(), nullable=False),
        sa.Column("application_config", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_applications_application_identifier"),
        "applications",
        ["application_identifier"],
        unique=True,
    )
    op.create_index(
        op.f("ix_applications_application_name"), "applications", ["application_name"], unique=False
    )
    op.create_index(
        op.f("ix_applications_application_owner_email"),
        "applications",
        ["application_owner_email"],
        unique=False,
    )
    op.create_index(op.f("ix_applications_identifier"), "applications", ["identifier"], unique=True)
    op.create_table(
        "job_scripts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_script_name", sa.String(), nullable=False),
        sa.Column("job_script_description", sa.String(), nullable=True),
        sa.Column("job_script_data_as_string", sa.String(), nullable=False),
        sa.Column("job_script_owner_email", sa.String(), nullable=False),
        sa.Column("application_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"],),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_job_scripts_job_script_name"), "job_scripts", ["job_script_name"], unique=False)
    op.create_index(
        op.f("ix_job_scripts_job_script_owner_email"), "job_scripts", ["job_script_owner_email"], unique=False
    )
    op.create_table(
        "job_submissions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_submission_name", sa.String(), nullable=False),
        sa.Column("job_submission_description", sa.String(), nullable=True),
        sa.Column("job_submission_owner_email", sa.String(), nullable=False),
        sa.Column("job_script_id", sa.Integer(), nullable=False),
        sa.Column("slurm_job_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["job_script_id"], ["job_scripts.id"],),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_job_submissions_job_submission_name"),
        "job_submissions",
        ["job_submission_name"],
        unique=False,
    )
    op.create_index(
        op.f("ix_job_submissions_job_submission_owner_email"),
        "job_submissions",
        ["job_submission_owner_email"],
        unique=False,
    )
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index(op.f("ix_job_submissions_job_submission_owner_email"), table_name="job_submissions")
    op.drop_index(op.f("ix_job_submissions_job_submission_name"), table_name="job_submissions")
    op.drop_table("job_submissions")
    op.drop_index(op.f("ix_job_scripts_job_script_owner_email"), table_name="job_scripts")
    op.drop_index(op.f("ix_job_scripts_job_script_name"), table_name="job_scripts")
    op.drop_table("job_scripts")
    op.drop_index(op.f("ix_applications_identifier"), table_name="applications")
    op.drop_index(op.f("ix_applications_application_owner_email"), table_name="applications")
    op.drop_index(op.f("ix_applications_application_name"), table_name="applications")
    op.drop_index(op.f("ix_applications_application_identifier"), table_name="applications")
    op.drop_table("applications")
    # ### end Alembic commands ###
