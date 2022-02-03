from dataclasses import dataclass
from datetime import datetime, timedelta
import functools
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import textwrap
import time
import traceback
from typing import Optional

import boto3
import click
from jose import jwt
from jose.exceptions import ExpiredSignatureError
from loguru import logger
import requests
import sentry_sdk
from tabulate import tabulate

from jobbergate_cli import client, constants
from jobbergate_cli.config import settings
from jobbergate_cli.jobbergate_api_wrapper import JobbergateApi


# These are used in help text for the application commands below
APPLICATION_ID_EXPLANATION = """

    This id represents the primary key of the application in the database. It
    will always be a unique integer and is automatically generated by the server
    when an Application is created. All applications receive an id, so it may
    be used to target a specific instance of an application whether or not it
    is provided with a human-friendly "identifier".
"""


APPLICATION_IDENTIFIER_EXPLANATION = """

    The identifier allows the user to access commonly used applications with a
    friendly name that is easy to remember. Identifiers should only be used
    for applications that are frequently used or should be easy to find in the list.
    An identifier may be added, removed, or changed on an existing application.
"""


@dataclass
class TokenSet:
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None


def init_logs(verbose=False):
    """
    Initialize the rotatating file log handler. Logs will be retained for 1 week.
    """
    # Remove default stderr handler at level INFO
    logger.remove()

    if verbose:
        logger.add(sys.stdout, level="DEBUG")

    logger.add(settings.JOBBERGATE_LOG_PATH, rotation="00:00", retention="1 week", level="DEBUG")
    logger.debug("Logging initialized")


def tabulate_response(response):
    """Print a tabulated json response"""
    if isinstance(response, list):
        text = tabulate((my_dict for my_dict in response), headers="keys")
    elif isinstance(response, dict):
        text = tabulate(response.items())
    else:
        text = str(response)
    print(text)


def raw_response(response):
    """Print a raw, pretty-printed json response"""
    if isinstance(response, (list, dict)):
        text = json.dumps(response, indent=2)
    else:
        text = str(response)
    print(text)


def jobbergate_command_wrapper(func):
    """Wraps a jobbergate command to include logging, error handling, and user output

    Reports the command being called and its parameters to the user log. Includes log
    lines about starting and finishing the command. Also reports errors to the user
    as well as sending the error report to Sentry. Finally, prints any output provided
    by the called command to stdout.
    """

    @functools.wraps(func)
    def wrapper(ctx, *args, **kwargs):
        try:
            message = f"Handling command '{ctx.command.name}'"
            if ctx.params:
                message += " with params:"
                for (key, value) in ctx.params.items():
                    message += f"\n  {key}={value}"
            logger.debug(message)

            result = func(ctx, *args, **kwargs)
            if result:
                if ctx.obj["raw"]:
                    raw_response(result)
                else:
                    tabulate_response(result)
            else:
                print("Received no data")

            logger.debug(f"Finished command '{ctx.command.name}'")
            return result

        except Exception as err:
            args_string = ", ".join(list(args) + [f"{k}={v}" for (k, v) in kwargs.items()])
            message = textwrap.dedent(
                f"""
                Caught error {err}:
                  identity: {ctx.obj["identity"]}
                  source:   {func.__name__}({args_string})
                  details:

                """
            ).lstrip()
            message += traceback.format_exc()

            logger.error(message)

            # This allows us to capture exceptions here and still report them to sentry
            if settings.SENTRY_DSN:
                with sentry_sdk.push_scope() as scope:
                    scope.set_context(
                        "command_info",
                        dict(
                            org_name=ctx.obj["identity"].get("org_name"),
                            user_email=ctx.obj["identity"]["user_email"],
                            function=func.__name__,
                            command=ctx.command.name,
                            args=args,
                            kwargs=kwargs,
                        ),
                    )
                    sentry_sdk.capture_exception(err)
                    sentry_sdk.flush()

            print(
                textwrap.dedent(
                    f"""
                    There was an error processing command '{ctx.command.name}'.

                    Please check the parameters and the command documentation.
                    You can check the documentation at any time by adding '--help' to any command.

                    If the problem persists, please contact Omnivector <info@omnivector.solutions>
                    for support.
                    """
                ).strip(),
                file=sys.stderr,
            )
            sys.exit(1)

    return wrapper


def abort_with_message(message):
    """
    Report an error to the user and exit the cli.
    """
    raise click.ClickException(textwrap.dedent(message).strip())


def validate_token_and_extract_identity(token: str) -> dict:
    """
    Validate the token and extract the identity data.

    Validations:
        * Checkstimestamp on the auth token.
        * Checks for identity data
        * Checks that all identity elements are present

    Reports an error in the logs and to the user if there is an issue with the token

    :param token: The JWT to use for auth on request to the API
    :returns: The extracted identity data
    """
    logger.debug("Validating access token")
    try:
        token_data = jwt.decode(
            token,
            None,
            options=dict(
                verify_signature=False,
                verify_aud=False,
                verify_exp=True,
            ),
        )

    except ExpiredSignatureError:
        raise  # Will be handled in calling context
    except Exception as err:
        logger.error(f"Unknown error while validating access token: {err}")
        if settings.SENTRY_DSN:
            with sentry_sdk.push_scope() as scope:
                scope.set_context("token", dict(token=token))
                sentry_sdk.capture_exception(err)
                sentry_sdk.flush()

        abort_with_message(
            """
            There was an unknown error while initializing the access token.

            Please try retrieving the auth token and logging in again.

            If the problem persists, please contact Omnivector <info@omnivector.solutions>
            for support.
            """
        )

    identity_data = token_data.get(settings.IDENTITY_CLAIMS_KEY)
    if not identity_data:
        abort_with_message("No identity data found in access token data")
    if "user_email" not in identity_data:
        abort_with_message("No user email found in access token data")

    return identity_data


def load_tokens_from_cache() -> TokenSet:
    """
    Loads an access token (and a refresh token if one exists) from the cache.

    :returns: A TokenSet instance containing the loaded tokens.
    """
    token_set = TokenSet()

    if not settings.JOBBERGATE_API_ACCESS_TOKEN_PATH.exists():
        abort_with_message("Please login with your auth token first using the `jobbergate login` command")

    logger.debug("Retrieving access token from cache")
    token_set.access_token = settings.JOBBERGATE_API_ACCESS_TOKEN_PATH.read_text()

    if settings.JOBBERGATE_API_REFRESH_TOKEN_PATH.exists():
        logger.debug("Retrieving refresh token from cache")
        token_set.refresh_token = settings.JOBBERGATE_API_REFRESH_TOKEN_PATH.read_text()

    return token_set


def save_tokens_to_cache(token_set: TokenSet):
    """
    Saves tokens from a token_set to the cache.

    :param token_set: A TokenSet instance containing the tokens to save.
    """
    logger.debug(f"Caching access token at {settings.JOBBERGATE_API_ACCESS_TOKEN_PATH}")
    settings.JOBBERGATE_API_ACCESS_TOKEN_PATH.write_text(token_set.access_token)

    if token_set.refresh_token is not None:
        logger.debug(f"Caching refresh token at {settings.JOBBERGATE_API_REFRESH_TOKEN_PATH}")
        settings.JOBBERGATE_API_REFRESH_TOKEN_PATH.write_text(token_set.refresh_token)


def clear_token_cache():
    """
    Clears the token cache.
    """
    logger.debug("Clearing cached tokens")

    logger.debug(f"Removing access token at {settings.JOBBERGATE_API_ACCESS_TOKEN_PATH}")
    if settings.JOBBERGATE_API_ACCESS_TOKEN_PATH.exists():
        settings.JOBBERGATE_API_ACCESS_TOKEN_PATH.unlink()

    logger.debug(f"Removing refresh token at {settings.JOBBERGATE_API_REFRESH_TOKEN_PATH}")
    if settings.JOBBERGATE_API_REFRESH_TOKEN_PATH.exists():
        settings.JOBBERGATE_API_REFRESH_TOKEN_PATH.unlink()


def init_access_token(ctx_obj):
    """
    Retrieves the access token for the user from the cache.

    Token is retrieved from the cache, validated, and identity data is bound to the context.

    If the access token is expired, a new one will be acquired via the cached refresh token (if there is one).

    :param ctx_obj: The click context object from the main entry point.
    :returns: the retrieved access token.
    """
    token_set = load_tokens_from_cache()
    try:
        identity_data = validate_token_and_extract_identity(token_set.access_token)

    except ExpiredSignatureError:
        if token_set.refresh_token is None:
            abort_with_message(
                """
                The auth token is expired. Please retrieve a new and log in again.

                If the problem persists, please contact Omnivector <info@omnivector.solutions>
                for support.
                """
            )

        logger.debug("The access token is expired. Attempting to refresh token")
        token_set.access_token = refresh_access_token(token_set.refresh_token)
        identity_data = validate_token_and_extract_identity(token_set.access_token)

    logger.debug(f"Executing with identity data: {identity_data}")
    ctx_obj["identity"] = identity_data

    save_tokens_to_cache(token_set)

    return token_set.access_token


def refresh_access_token(refresh_token: str) -> str:
    """
    Attempt to fetch a new access token given a refresh token.

    If refresh fails, notify the user that they need to log in again.

    :param refresh_token: The refresh token to be used to retrieve a new access token.
    :returns: The fetched access token.
    """
    url = f"https://{settings.AUTH0_DOMAIN}/oauth/token"
    logger.debug(f"Requesting refreshed access token from {url}")
    response = requests.post(
        url,
        headers={"content-type": "application/x-www-form-urlencoded"},
        data=dict(
            client_id=settings.AUTH0_CLIENT_ID,
            audience=settings.AUTH0_AUDIENCE,
            grant_type="refresh_token",
            refresh_token=refresh_token,
        ),
    )
    data = response.json()
    if response.status_code != 200:
        logger.debug(f"Error for refresh request: {data['error_description']}")
        abort_with_message(
            """
            The auth token could not be refreshed. Please try logging in again.

            If the problem persists, please contact Omnivector <info@omnivector.solutions>
            for support.
            """
        )
    access_token = data["access_token"]
    return access_token


def fetch_auth_tokens() -> TokenSet:
    """
    Fetch an access token (and possibly a refresh token) from Auth0.

    Prints out a URL for the user to use to authenticate and polls the token endpoint to fetch it when
    the browser-based process finishes

    :returns: A TokenSet object carrying the fetched tokens.
    """
    url = f"https://{settings.AUTH0_DOMAIN}/oauth/device/code"
    response = requests.post(
        url,
        headers={"content-type": "application/x-www-form-urlencoded"},
        data=dict(
            client_id=settings.AUTH0_CLIENT_ID,
            audience=settings.AUTH0_AUDIENCE,
            scope="offline_access",  # To get refresh token
        ),
    )

    try:
        data = response.json()
    except Exception:
        abort_with_message("Failed unpacking response for verification code")

    data = response.json()
    logger.debug(f"Response for device code request: {data}")
    if response.status_code != 200:
        error_message = data.get("error", "unknown error")
        abort_with_message(f"Could not authenticate: {error_message}")

    device_code = data["device_code"]
    verification_uri_complete = data["verification_uri_complete"]
    sleep_interval = data["interval"]

    print()
    print("To complete login, please open the following link in a browser:")
    print()
    print(f"  {verification_uri_complete}")
    print()
    print(f"Waiting up to {settings.AUTH0_MAX_POLL_TIME / 60} minutes for you to complete the process...")

    time_limit = timedelta(seconds=settings.AUTH0_MAX_POLL_TIME)
    start_time = datetime.now()
    attempt_count = 0
    while start_time + time_limit > datetime.now():
        attempt_count += 1
        token_url = f"https://{settings.AUTH0_DOMAIN}/oauth/token"
        token_response = requests.post(
            token_url,
            headers={"content-type": "application/x-www-form-urlencoded"},
            data=dict(
                grant_type="urn:ietf:params:oauth:grant-type:device_code",
                device_code=device_code,
                client_id=settings.AUTH0_CLIENT_ID,
            ),
        )

        try:
            data = token_response.json()
        except Exception:
            abort_with_message("Failed unpacking response for tokens")

        logger.debug(f"Response for device token request: {data}")
        if token_response.status_code != 200:
            error_message = data.get("error", "unknown error")
            logger.debug(f"Token fetch attempt #{attempt_count} failed: {error_message}")
            time.sleep(sleep_interval)
            continue

        token_set = TokenSet(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
        )
        return token_set

    abort_with_message(
        """
        Timed out while waiting for user to complete login. Please try again.

        If the problem persists, please contact Omnivector <info@omnivector.solutions>
        for support.
        """
    )


def init_sentry():
    """Initialize Sentry."""
    logger.debug("Initializing sentry")
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        traces_sample_rate=1.0,
    )


# Get the cli input arguments
@click.group(
    help="""
        Jobbergate CLI.

        Provides a command-line interface to the Jobbergate API. Available commands are
        listed below. Each command may be invoked with --help to see more details and
        available parameters.

        Before you use any commands besides ``login`` or ``logout``, you must first log in
        by invoking the ``login`` subcommand with an auth token retrieved from the Armada UI.
        Once you login, the token will be cached for future commands until it expires. If the
        token has expired, you will be notified that you need to supply a new one.
    """,
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Enable verbose logging to the terminal",
)
@click.option(
    "--raw",
    "-r",
    is_flag=True,
    help="Print output as raw json",
)
@click.option(
    "--full",
    "-f",
    is_flag=True,
    help="Print all columns. Must be used with --raw",
)
@click.version_option()
@click.pass_context
def main(ctx, verbose, raw, full):
    ctx.ensure_object(dict)

    init_logs(verbose=verbose)

    if full and not raw:
        abort_with_message("--full option must be used with --raw")

    if settings.SENTRY_DSN:
        logger.debug(f"Initializing Sentry with {settings.SENTRY_DSN}")
        init_sentry()

    if ctx.invoked_subcommand not in ("login", "logout"):
        token = init_access_token(ctx.obj)
        user_email = ctx.obj["identity"]["user_email"]

        if settings.JOBBERGATE_DEBUG:
            logger.debug("Enabling debug mode for requests")
            client.debug_requests_on()

        ctx.obj["api"] = JobbergateApi(
            token=token,
            job_script_config=constants.JOBBERGATE_JOB_SCRIPT_CONFIG,
            job_submission_config=constants.JOBBERGATE_JOB_SUBMISSION_CONFIG,
            application_config=constants.JOBBERGATE_APPLICATION_CONFIG,
            api_endpoint=settings.JOBBERGATE_API_ENDPOINT,
            user_email=user_email,
            full_output=full,
        )
        ctx.obj["raw"] = raw


@main.command("list-applications")
@click.option(
    "--all",
    is_flag=True,
    help="Show all applications, even the ones without identifier",
)
@click.option(
    "--user",
    is_flag=True,
    help="Show only the applications for the current user",
)
@click.pass_context
@jobbergate_command_wrapper
def list_applications(ctx, all=False, user=False):
    """
    LIST the available applications.
    """
    api = ctx.obj["api"]
    return api.list_applications(all, user)


@main.command("create-application")
@click.option("--name", "-n", help="Name of the application")
@click.option(
    "--identifier",
    help=f"The human-friendly identifier of the application. {APPLICATION_IDENTIFIER_EXPLANATION}",
)
@click.option(
    "--application-path",
    "-a",
    help="The path to the directory where application files are",
)
@click.option(
    "--application-desc",
    default="",
    help="A helpful description of the application",
)
@click.pass_context
@jobbergate_command_wrapper
def create_application(
    ctx,
    name,
    identifier,
    application_path,
    application_desc,
):
    """
    CREATE an application.
    """
    api = ctx.obj["api"]
    return api.create_application(
        application_name=name,
        application_identifier=identifier,
        application_path=application_path,
        application_desc=application_desc,
    )


@main.command("get-application")
@click.option(
    "--id",
    "-i",
    "id_",
    help=f"The specific id of the application. {APPLICATION_ID_EXPLANATION}",
)
@click.option(
    "--identifier",
    help=f"The human-friendly identifier of the application. {APPLICATION_IDENTIFIER_EXPLANATION}",
)
@click.pass_context
@jobbergate_command_wrapper
def get_application(ctx, id_, identifier):
    """
    GET an Application.
    """
    api = ctx.obj["api"]
    return api.get_application(application_id=id_, application_identifier=identifier)


@main.command("update-application")
@click.option(
    "--id",
    "-i",
    "id_",
    help=f"The specific id application to update. {APPLICATION_ID_EXPLANATION}",
)
@click.option(
    "--identifier",
    help=f"The human-friendly identifier of the application to update. {APPLICATION_IDENTIFIER_EXPLANATION}",
)
@click.option(
    "--application-path",
    "-a",
    help="The path to the directory for updated application files",
)
@click.option("--update-identifier", help="The application identifier to be set")
@click.option(
    "--application-desc",
    default="",
    help="Optional new application description",
)
@click.pass_context
@jobbergate_command_wrapper
def update_application(
    ctx,
    id_,
    identifier,
    application_path,
    update_identifier,
    application_desc,
):
    """
    UPDATE an Application.
    """
    api = ctx.obj["api"]
    return api.update_application(id_, identifier, application_path, update_identifier, application_desc)


@main.command("delete-application")
@click.option(
    "--id",
    "-i",
    "id_",
    help=f"The specific id of the application to delete. {APPLICATION_ID_EXPLANATION}",
)
@click.option(
    "--identifier",
    help=f"The human-friendly identifier of the application to delete. {APPLICATION_IDENTIFIER_EXPLANATION}",
)
@click.pass_context
@jobbergate_command_wrapper
def delete_application(ctx, id_, identifier):
    """
    DELETE an Application.
    """
    api = ctx.obj["api"]
    return api.delete_application(id_, identifier)


@main.command("list-job-scripts")
@click.option(
    "--all",
    "all_",
    is_flag=True,
    help="""
        Optional parameter that will return all job scripts.
        If NOT specified then only the user's job scripts will be returned.
    """,
)
@click.pass_context
@jobbergate_command_wrapper
def list_job_scripts(ctx, all_=False):
    """
    LIST Job Scripts.
    """
    api = ctx.obj["api"]
    return api.list_job_scripts(all_)


@main.command("create-job-script")
@click.option(
    "--name",
    "-n",
    default="default_script_name",
    help="Name for job script",
)
@click.option(
    "--application-id",
    "-i",
    help="The id of the application for the job script",
)
@click.option(
    "--application-identifier",
    help="The identifier of the application for the job script",
)
@click.option(
    "--sbatch-params",
    multiple=True,
    help="Optional parameter to submit raw sbatch parameters",
)
@click.option(
    "--param-file",
    type=click.Path(),
    help="""
        Optional parameter file for populating templates.
        If answers are not provided, the question asking in jobbergate.py is triggered
    """,
)
@click.option(
    "--fast",
    "-f",
    is_flag=True,
    help="""
        Optional parameter to use default answers (when available)
        instead of asking user.
    """,
)
@click.option(
    "--no-submit",
    is_flag=True,
    help="Optional parameter to not even ask about submitting job",
)
@click.option(
    "--debug",
    is_flag=True,
    help="Optional parameter to view job script data in CLI output",
)
@click.pass_context
@jobbergate_command_wrapper
def create_job_script(
    ctx,
    name,
    application_id,
    application_identifier,
    sbatch_params,
    param_file=None,
    fast=False,
    no_submit=False,
    debug=False,
):
    """
    CREATE a Job Script.
    """
    api = ctx.obj["api"]
    return api.create_job_script(
        name,
        application_id,
        application_identifier,
        param_file,
        sbatch_params,
        fast,
        no_submit,
        debug,
    )


@main.command("get-job-script")
@click.option(
    "--id",
    "-i",
    "id_",
    help="The id of job script to be returned",
)
@click.option(
    "--as-string",
    is_flag=True,
)
@click.pass_context
@jobbergate_command_wrapper
def get_job_script(ctx, id_, as_string):
    """
    GET a Job Script.
    """
    api = ctx.obj["api"]
    return api.get_job_script(id_, as_string)


@main.command("update-job-script")
@click.option(
    "--id",
    "-i",
    "id_",
    help="The id of the job script to update",
)
@click.option(
    "--job-script",
    help="""
        The data with which to update job script.

        Format: string form of dictionary with main script as entry "application.sh"

        Example: '{"application.sh":"#!/bin/bash \\n hostname"}'
    """,
)
@click.pass_context
@jobbergate_command_wrapper
def update_job_script(ctx, id_, job_script):
    """
    UPDATE a Job Script.
    """
    api = ctx.obj["api"]
    return api.update_job_script(id_, job_script)


@main.command("delete-job-script")
@click.option("--id", "-i", "id_", help="The id of job script to delete")
@click.pass_context
@jobbergate_command_wrapper
def delete_job_script(ctx, id_):
    """
    DELETE a Job Script.
    """
    api = ctx.obj["api"]
    return api.delete_job_script(id_)


@main.command("list-job-submissions")
@click.option(
    "--all",
    "all_",
    is_flag=True,
    help="""
        Optional parameter that will return all job submissions.
        If NOT specified then only the user's job submissions will be returned.
    """,
)
@click.pass_context
@jobbergate_command_wrapper
def list_job_submissions(ctx, all_=False):
    """
    LIST Job Submissions.
    """
    api = ctx.obj["api"]
    return api.list_job_submissions(all_)


@main.command("create-job-submission")
@click.option(
    "--job-script-id",
    "-i",
    help="The id of the job script to submit",
)
@click.option(
    "--name",
    "-n",
    help="The name for job submission",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="""
        Optional flag that will create record in API and return data to CLI but
        WILL NOT submit job
    """,
)
@click.pass_context
@jobbergate_command_wrapper
def create_job_submission(
    ctx,
    job_script_id,
    name="",
    dry_run=False,
):
    """
    CREATE Job Submission.
    """
    api = ctx.obj["api"]
    return api.create_job_submission(
        job_script_id=job_script_id, job_submission_name=name, render_only=dry_run
    )


@main.command("get-job-submission")
@click.option("--id", "-i", "id_", help="The id of the job submission to be returned")
@click.pass_context
@jobbergate_command_wrapper
def get_job_submission(ctx, id_):
    """
    GET a Job Submission.
    """
    api = ctx.obj["api"]
    return api.get_job_submission(id_)


@main.command("update-job-submission")
@click.option("--id", "-i", "id_", help="The id of job submission to update")
@click.pass_context
@jobbergate_command_wrapper
def update_job_submission(ctx, id_):
    """
    UPDATE a Job Submission.
    """
    api = ctx.obj["api"]
    return api.update_job_submission(id_)


@main.command("delete-job-submission")
@click.option("--id", "-i", "id_", help="The id of job submission to delete")
@click.pass_context
@jobbergate_command_wrapper
def delete_job_submission(ctx, id_):
    """
    DELETE a Job Submission.
    """
    api = ctx.obj["api"]
    return api.delete_job_submission(id_)


@main.command("upload-logs")
@click.pass_context
@jobbergate_command_wrapper
def upload_logs(ctx):
    """
    Uploads user logs to S3 for analysis. Should only be used after an incident that was
    reported to the Jobbergate support team.
    """
    logger.debug("Initializing S3 client")
    s3_client = boto3.client(
        "s3",
        aws_access_key_id=settings.JOBBERGATE_AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.JOBBERGATE_AWS_SECRET_ACCESS_KEY,
    )

    tarball_name = "{user}.{timestamp}.tar.gz".format(
        user=ctx.obj["token"]["username"],
        timestamp=datetime.utcnow().strftime("%Y%m%d.%H%M%S"),
    )

    logger.debug("Creating tarball of user's logs")
    log_dir = settings.JOBBERGATE_LOG_PATH.parent
    with tempfile.TemporaryDirectory() as temp_dir:
        tarball_path = Path(temp_dir) / tarball_name
        with tarfile.open(tarball_path, "w:gz") as tarball:
            for filename in log_dir.iterdir():
                if filename.match(f"{settings.JOBBERGATE_LOG_PATH}*"):
                    tarball.add(str(filename))

        logger.debug(f"Uploading {tarball_name} to S3")
        s3_client.upload_file(str(tarball_path), settings.JOBBERGATE_S3_LOG_BUCKET, tarball_name)

    return "Upload complete. Please notify Omnivector <info@omnivector.solutions>."


@main.command("login")
@click.option(
    "--token",
    "-t",
    help=textwrap.dedent(
        """
        Supply a token instead of fetching one.
        This will clear any cached refresh tokens and automatic refresh will be unavailable until you
        login in again without an explicit access token.
        """
    ).strip(),
)
def login(token=None, refresh_token=None):
    """
    Log in to the jobbergate-cli by storing the supplied token argument in the cache.
    """
    if token is not None:
        clear_token_cache()
        token_set = TokenSet(access_token=token)
    else:
        token_set = fetch_auth_tokens()
    identity_data = validate_token_and_extract_identity(token_set.access_token)
    save_tokens_to_cache(token_set)
    print()
    print(f"Logged in user {identity_data['user_email']}")


@main.command("logout")
def logout():
    """
    Logs out of the jobbergate-cli. Clears the saved user credentials.
    """
    clear_token_cache()
    print("Cleared cached tokens")


if __name__ == "__main__":
    main()
