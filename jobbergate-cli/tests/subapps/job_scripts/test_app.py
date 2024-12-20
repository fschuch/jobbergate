import importlib
import json
import pathlib
import shlex
from unittest import mock

import httpx
import pytest

from jobbergate_cli.config import settings
from jobbergate_cli.schemas import ApplicationResponse, JobScriptFile, JobScriptResponse, JobSubmissionResponse
from jobbergate_cli.subapps.job_scripts.app import (
    HIDDEN_FIELDS,
    JOB_SUBMISSION_HIDDEN_FIELDS,
    clone,
    create,
    create_locally,
    create_stand_alone,
    delete,
    download_files,
    get_one,
    list_all,
    show_files,
    style_mapper,
    update,
)
from jobbergate_cli.text_tools import unwrap


def test_list_all__renders_paginated_results(
    make_test_app,
    dummy_context,
    cli_runner,
    mocker,
):
    test_app = make_test_app("list-all", list_all)
    mocked_pagination = mocker.patch("jobbergate_cli.subapps.job_scripts.app.handle_pagination")
    result = cli_runner.invoke(test_app, ["list-all"])
    assert result.exit_code == 0, f"list-all failed: {result.stdout}"
    mocked_pagination.assert_called_once_with(
        jg_ctx=dummy_context,
        url_path="/jobbergate/job-scripts",
        abort_message="Couldn't retrieve job scripts list from API",
        params={"user_only": True, "sort_ascending": False, "sort_field": "id"},
        title="Job Scripts List",
        style_mapper=style_mapper,
        hidden_fields=HIDDEN_FIELDS,
        nested_response_model_cls=JobScriptResponse,
    )


@pytest.mark.parametrize("selector_template", ["{id}", "-i {id}", "--id={id}", "--id {id}"])
def test_get_one__success(
    respx_mock,
    make_test_app,
    dummy_context,
    dummy_job_script_data,
    dummy_domain,
    cli_runner,
    mocker,
    selector_template,
):
    job_script_data = dummy_job_script_data[0]
    id = job_script_data["id"]

    cli_selector = selector_template.format(id=id)

    respx_mock.get(f"{dummy_domain}/jobbergate/job-scripts/{id}").mock(
        return_value=httpx.Response(
            httpx.codes.OK,
            json=dummy_job_script_data[0],
        ),
    )
    test_app = make_test_app("get-one", get_one)
    mocked_render = mocker.patch("jobbergate_cli.subapps.job_scripts.app.render_single_result")
    result = cli_runner.invoke(test_app, shlex.split(f"get-one {cli_selector}"))
    assert result.exit_code == 0, f"get-one failed: {result.stdout}"
    mocked_render.assert_called_once_with(
        dummy_context,
        JobScriptResponse.model_validate(dummy_job_script_data[0]),
        title="Job Script",
        hidden_fields=HIDDEN_FIELDS,
    )


def test_create_stand_alone__success(
    respx_mock,
    make_test_app,
    dummy_context,
    dummy_domain,
    dummy_render_class,
    cli_runner,
    tmp_path,
    attach_persona,
    mocker,
):
    dummy_job_script = tmp_path / "dummy.sh"
    dummy_job_script.write_text("echo hello world")

    dummy_support_1 = tmp_path / "dummy-support-1.txt"
    dummy_support_1.write_text("dummy 1")

    dummy_support_2 = tmp_path / "dummy-support-2.txt"
    dummy_support_2.write_text("dummy 2")

    create_route = respx_mock.post(f"{dummy_domain}/jobbergate/job-scripts")
    create_route.mock(
        return_value=httpx.Response(
            httpx.codes.CREATED,
            json=dict(
                id=1,
                created_at="2023-10-03 08:25:00",
                updated_at="2023-10-03 08:25:00",
                name="dummy-name",
                description=None,
                owner_email="dummy@dummy.com",
                application_id=None,
            ),
        )
    )

    upload_entrypoint_route = respx_mock.put(f"{dummy_domain}/jobbergate/job-scripts/1/upload/ENTRYPOINT")
    upload_entrypoint_route.mock(return_value=httpx.Response(httpx.codes.OK))

    upload_support_route = respx_mock.put(
        f"{dummy_domain}/jobbergate/job-scripts/1/upload/SUPPORT",
    )
    upload_support_route.mock(return_value=httpx.Response(httpx.codes.OK))

    attach_persona("dummy@dummy.com")

    test_app = make_test_app("create-stand-alone", create_stand_alone)

    mocked_render = mocker.patch("jobbergate_cli.subapps.job_scripts.app.render_single_result")
    mocker.patch.object(
        importlib.import_module("inquirer.prompt"),
        "ConsoleRender",
        new=dummy_render_class,
    )
    result = cli_runner.invoke(
        test_app,
        shlex.split(
            unwrap(
                f"""
                create-stand-alone {dummy_job_script} --name=dummy-name
                       --supporting-file={dummy_support_1}
                       --supporting-file={dummy_support_2}
                """
            )
        ),
    )
    assert result.exit_code == 0, f"create failed: {result.stdout}"

    assert create_route.called
    content = json.loads(create_route.calls.last.request.content)
    assert content == {
        "name": "dummy-name",
        "description": None,
    }

    assert upload_entrypoint_route.call_count == 1
    assert b'filename="dummy.sh"' in upload_entrypoint_route.calls[0].request.content

    assert upload_support_route.call_count == 2
    assert b'filename="dummy-support-1.txt"' in upload_support_route.calls[0].request.content
    assert b'filename="dummy-support-2.txt"' in upload_support_route.calls[1].request.content

    mocked_render.assert_called_once_with(
        dummy_context,
        JobScriptResponse.model_validate(
            dict(
                id=1,
                created_at="2023-10-03 08:25:00",
                updated_at="2023-10-03 08:25:00",
                name="dummy-name",
                description=None,
                owner_email="dummy@dummy.com",
                application_id=None,
            ),
        ),
        title="Created Job Script",
        hidden_fields=HIDDEN_FIELDS,
    )


@pytest.mark.parametrize(
    "selector_template",
    ["{id}", "-i {id}", "--application-id={id}", "--application-id {id}"],
)
def test_create__non_fast_mode_and_job_submission(
    respx_mock,
    make_test_app,
    dummy_context,
    dummy_module_source,
    dummy_application_data,
    dummy_job_script_data,
    dummy_job_submission_data,
    dummy_domain,
    dummy_render_class,
    cli_runner,
    tmp_path,
    attach_persona,
    mocker,
    selector_template,
):
    application_response = ApplicationResponse(**dummy_application_data[0])
    id = application_response.application_id
    identifier = application_response.identifier

    url_selector = identifier if "identifier" in selector_template else id
    cli_selector = selector_template.format(id=id, identifier=identifier)

    job_script_data = dummy_job_script_data[0]

    job_submission_data = dummy_job_submission_data[0]

    render_route = respx_mock.post(f"{dummy_domain}/jobbergate/job-scripts/render-from-template/{url_selector}")
    render_route.mock(
        return_value=httpx.Response(
            httpx.codes.CREATED,
            json=job_script_data,
        ),
    )

    sbatch_params = " ".join(f"--sbatch-params={i}" for i in (1, 2, 3))

    param_file_path = tmp_path / "param_file.json"
    param_file_path.write_text(json.dumps(dict(foo="oof")))

    dummy_render_class.prepared_input = dict(
        foo="FOO",
        bar="BAR",
        baz="BAZ",
    )

    attach_persona("dummy@dummy.com")

    test_app = make_test_app("create", create)
    mocked_render = mocker.patch("jobbergate_cli.subapps.job_scripts.app.render_single_result")
    mocked_fetch_application_data = mocker.patch(
        "jobbergate_cli.subapps.job_scripts.tools.fetch_application_data",
        return_value=application_response,
    )
    assert len(application_response.workflow_files) >= 1
    get_workflow_route = respx_mock.get(f"{dummy_domain}{application_response.workflow_files[0].path}")
    get_workflow_route.mock(
        return_value=httpx.Response(
            httpx.codes.OK,
            content=dummy_module_source.encode(),
        ),
    )

    submissions_handler = mock.MagicMock()
    submissions_handler.run.return_value = JobSubmissionResponse.model_validate(job_submission_data)
    mocked_factory = mocker.patch(
        "jobbergate_cli.subapps.job_scripts.app.job_submissions_factory", return_value=submissions_handler
    )

    mocker.patch.object(
        importlib.import_module("inquirer.prompt"),
        "ConsoleRender",
        new=dummy_render_class,
    )
    result = cli_runner.invoke(
        test_app,
        shlex.split(
            unwrap(
                f"""
                create {cli_selector}
                       --name dummy-name
                       --param-file={param_file_path}
                       {sbatch_params}
                """
            )
        ),
        # To confirm that the job should be submitted to the default cluster, in the current dir and not downloaded
        input=f"y\nn\n{settings.DEFAULT_CLUSTER_NAME}\n.\n",
    )
    assert result.exit_code == 0, f"create failed: {result.stdout}"
    mocked_fetch_application_data.assert_called_once_with(
        dummy_context,
        application_response.application_id,
    )

    assert render_route.call_count == 1
    content = json.loads(render_route.calls.last.request.content)
    assert content == {
        "create_request": {"name": "dummy-name", "description": None},
        "render_request": {
            "template_output_name_mapping": {"test-job-script.py.j2": "test-job-script.py"},
            "sbatch_params": ["1", "2", "3"],
            "param_dict": {
                "data": {
                    "foo": "oof",
                    "bar": "BAR",
                    "baz": "BAZ",
                    "template_files": None,
                    "default_template": "test-job-script.py.j2",
                    "supporting_files_output_name": None,
                    "supporting_files": None,
                }
            },
        },
    }

    mocked_factory.assert_called_once_with(
        jg_ctx=dummy_context,
        job_script_id=job_script_data["id"],
        name=job_script_data["name"],
        description=job_script_data["description"],
        cluster_name=None,
        execution_directory=None,
        sbatch_arguments=None,
    )

    mocked_render.assert_has_calls(
        [
            mocker.call(
                dummy_context,
                JobScriptResponse(**job_script_data),
                title="Created Job Script",
                hidden_fields=HIDDEN_FIELDS,
            ),
            mocker.call(
                dummy_context,
                JobSubmissionResponse(**job_submission_data),
                title="Created Job Submission (Fast Mode)",
                hidden_fields=JOB_SUBMISSION_HIDDEN_FIELDS,
            ),
        ]
    )


def test_create__with_fast_mode_and_no_job_submission(
    respx_mock,
    make_test_app,
    dummy_context,
    dummy_module_source,
    dummy_application_data,
    dummy_job_script_data,
    dummy_domain,
    cli_runner,
    tmp_path,
    attach_persona,
    mocker,
):
    application_response = ApplicationResponse(**dummy_application_data[0])

    job_script_data = dummy_job_script_data[0]

    render_route = respx_mock.post(
        f"{dummy_domain}/jobbergate/job-scripts/render-from-template/{application_response.application_id}"
    )
    render_route.mock(
        return_value=httpx.Response(
            httpx.codes.CREATED,
            json=job_script_data,
        ),
    )

    sbatch_params = " ".join(f"--sbatch-params={i}" for i in (1, 2, 3))

    param_file_path = tmp_path / "param_file.json"
    param_file_path.write_text(
        json.dumps(
            dict(
                foo="oof",
                bar="rab",
                baz="zab",
            )
        )
    )

    attach_persona("dummy@dummy.com")

    test_app = make_test_app("create", create)
    mocked_render = mocker.patch("jobbergate_cli.subapps.job_scripts.app.render_single_result")
    mocked_fetch_application_data = mocker.patch(
        "jobbergate_cli.subapps.job_scripts.tools.fetch_application_data",
        return_value=application_response,
    )
    assert len(application_response.workflow_files) >= 1
    get_workflow_route = respx_mock.get(f"{dummy_domain}{application_response.workflow_files[0].path}")
    get_workflow_route.mock(
        return_value=httpx.Response(
            httpx.codes.OK,
            content=dummy_module_source.encode(),
        ),
    )
    result = cli_runner.invoke(
        test_app,
        shlex.split(
            unwrap(
                f"""
                create --name=dummy-name
                       --application-id={application_response.application_id}
                       --param-file={param_file_path}
                       --fast
                       --no-submit
                       --no-download
                       {sbatch_params}
                """
            )
        ),
    )
    assert result.exit_code == 0, f"create failed: {result.stdout}"
    mocked_fetch_application_data.assert_called_once_with(dummy_context, application_response.application_id)
    assert render_route.call_count == 1
    content = json.loads(render_route.calls.last.request.content)
    assert content == {
        "create_request": {"name": "dummy-name", "description": None},
        "render_request": {
            "template_output_name_mapping": {"test-job-script.py.j2": "test-job-script.py"},
            "sbatch_params": ["1", "2", "3"],
            "param_dict": {
                "data": {
                    "foo": "oof",
                    "bar": "rab",
                    "baz": "zab",
                    "template_files": None,
                    "default_template": "test-job-script.py.j2",
                    "supporting_files_output_name": None,
                    "supporting_files": None,
                }
            },
        },
    }

    mocked_render.assert_called_once_with(
        dummy_context,
        JobScriptResponse(**job_script_data),
        title="Created Job Script",
        hidden_fields=HIDDEN_FIELDS,
    )


def test_create__submit_is_none_and_cluster_name_is_defined(
    respx_mock,
    make_test_app,
    dummy_context,
    dummy_application_data,
    dummy_job_script_data,
    dummy_domain,
    cli_runner,
    tmp_path,
    attach_persona,
    mocker,
):
    application_response = ApplicationResponse(**dummy_application_data[0])

    job_script_data = dummy_job_script_data[0]

    render_route = respx_mock.post(
        f"{dummy_domain}/jobbergate/job-scripts/render-from-template/{application_response.application_id}"
    )
    render_route.mock(
        return_value=httpx.Response(httpx.codes.CREATED, json=job_script_data),
    )

    sbatch_params = " ".join(f"--sbatch-params={i}" for i in (1, 2, 3))

    param_file_path = tmp_path / "param_file.json"
    param_file_path.write_text(
        json.dumps(
            dict(
                foo="oof",
                bar="rab",
                baz="zab",
            )
        )
    )

    attach_persona("dummy@dummy.com")

    test_app = make_test_app("create", create)
    mocked_render = mocker.patch("jobbergate_cli.subapps.job_scripts.app.render_single_result")
    mocked_fetch_application_data = mocker.patch(
        "jobbergate_cli.subapps.job_scripts.app.render_job_script",
        return_value=JobScriptResponse(**job_script_data),
    )

    result = cli_runner.invoke(
        test_app,
        shlex.split(
            unwrap(
                f"""
                create --name=dummy-name
                       --application-id={application_response.application_id}
                       --param-file={param_file_path}
                       --cluster-name=dummy-cluster
                       {sbatch_params}
                """
            )
        ),
    )
    assert mocked_fetch_application_data.call_count == 1
    mocked_render.assert_called_once_with(
        dummy_context,
        JobScriptResponse(**job_script_data),
        title="Created Job Script",
        hidden_fields=HIDDEN_FIELDS,
    )

    assert result.exit_code == 1, f"create failed: {result.stdout}"
    assert "Incorrect parameters" in result.stdout


def test_create_job_script_locally__success(
    dummy_render_class, dummy_application_dir, make_test_app, cli_runner, mocker
):
    test_app = make_test_app("create-locally", create_locally)

    dummy_render_class.prepared_input = dict(
        foo="FOO",
        bar="BAR",
        baz="BAZ",
    )
    mocker.patch.object(
        importlib.import_module("inquirer.prompt"),
        "ConsoleRender",
        new=dummy_render_class,
    )

    mocked_terminal_message = mocker.patch("jobbergate_cli.subapps.job_scripts.app.terminal_message")

    result = cli_runner.invoke(
        test_app,
        shlex.split(f"create-locally {dummy_application_dir} --output-path={dummy_application_dir}"),
    )

    assert result.exit_code == 0, f"create-locally failed: {result.stdout}"

    mocked_terminal_message.assert_called_once_with(
        "The job script was successfully rendered locally.",
        subject="Job script render succeeded",
    )


@pytest.mark.parametrize("selector_template", ["{id}", "-i {id}", "--id={id}", "--id {id}"])
def test_update__makes_request_and_renders_results(
    respx_mock,
    make_test_app,
    dummy_context,
    dummy_job_script_data,
    dummy_domain,
    cli_runner,
    mocker,
    selector_template,
):
    job_script_data = dummy_job_script_data[0]
    job_script_id = job_script_data["id"]

    cli_selector = selector_template.format(id=job_script_id)

    new_job_script_data = {
        **job_script_data,
        "name": "new-test-name",
        "description": "new-test-description",
    }
    respx_mock.put(f"{dummy_domain}/jobbergate/job-scripts/{job_script_id}").mock(
        return_value=httpx.Response(httpx.codes.OK, json=new_job_script_data),
    )
    test_app = make_test_app("update", update)
    mocked_render = mocker.patch("jobbergate_cli.subapps.job_scripts.app.render_single_result")
    result = cli_runner.invoke(
        test_app,
        shlex.split(
            unwrap(
                f"""
                update {cli_selector}
                       --name='new-test-name'
                       --description='new-test-description'
                """
            )
        ),
    )
    assert result.exit_code == 0, f"update failed: {result.stdout}"
    mocked_render.assert_called_once_with(
        dummy_context,
        JobScriptResponse(**new_job_script_data),
        title="Updated Job Script",
        hidden_fields=HIDDEN_FIELDS,
    )


@pytest.mark.parametrize("selector_template", ["{id}", "-i {id}", "--id={id}", "--id {id}"])
def test_delete__makes_request_and_sends_terminal_message(
    respx_mock,
    make_test_app,
    dummy_domain,
    cli_runner,
    selector_template,
):
    job_script_id = 13

    cli_selector = selector_template.format(id=job_script_id)

    delete_route = respx_mock.delete(f"{dummy_domain}/jobbergate/job-scripts/{job_script_id}").mock(
        return_value=httpx.Response(httpx.codes.NO_CONTENT),
    )
    test_app = make_test_app("delete", delete)
    result = cli_runner.invoke(test_app, shlex.split(f"delete {cli_selector}"))
    assert result.exit_code == 0, f"delete failed: {result.stdout}"
    assert delete_route.called
    assert "JOB SCRIPT DELETE SUCCEEDED"


@pytest.mark.parametrize("selector_template", ["{id}", "-i {id}", "--id={id}", "--id {id}"])
def test_show_files__success(
    respx_mock,
    make_test_app,
    dummy_job_script_data,
    dummy_domain,
    dummy_template_source,
    cli_runner,
    mocker,
    selector_template,
):
    """
    Verify that the ``show-files`` subcommand works as expected.
    """
    job_script_data = dummy_job_script_data[0]
    id = job_script_data["id"]

    cli_selector = selector_template.format(id=id)

    respx_mock.get(f"{dummy_domain}/jobbergate/job-scripts/{id}").mock(
        return_value=httpx.Response(
            httpx.codes.OK,
            json=job_script_data,
        ),
    )

    get_file_routes = [
        respx_mock.get(f"{dummy_domain}{JobScriptFile.model_validate(f).path}") for f in job_script_data["files"]
    ]
    for route in get_file_routes:
        route.mock(
            return_value=httpx.Response(
                httpx.codes.OK,
                content=dummy_template_source.encode(),
            ),
        )

    test_app = make_test_app("show-files", show_files)
    mocked_terminal_message = mocker.patch("jobbergate_cli.subapps.job_scripts.app.terminal_message")

    result = cli_runner.invoke(test_app, shlex.split(f"show-files {cli_selector}"))
    assert result.exit_code == 0, f"get-one failed: {result.stdout}"
    mocked_terminal_message.assert_called_once_with(
        dummy_template_source,
        subject="application.sh",
        footer="This is the main job script file",
    )


class TestDownloadJobScriptFiles:
    """
    Test the ``download`` subcommand.
    """

    @pytest.fixture()
    def test_app(self, make_test_app):
        """
        Fixture to create a test app.
        """
        return make_test_app("download", download_files)

    @pytest.mark.parametrize("selector_template", ["{id}", "-i {id}", "--id={id}", "--id {id}"])
    def test_download__success(
        self,
        respx_mock,
        test_app,
        dummy_job_script_data,
        dummy_domain,
        dummy_context,
        cli_runner,
        mocker,
        tmp_path,
        selector_template,
    ):
        """
        Test that the ``download`` subcommand works as expected.
        """
        job_script_data = dummy_job_script_data[0]
        id = job_script_data["id"]

        cli_selector = selector_template.format(id=id)

        respx_mock.get(f"{dummy_domain}/jobbergate/job-scripts/{id}").mock(
            return_value=httpx.Response(
                httpx.codes.OK,
                json=job_script_data,
            ),
        )
        mocked_render = mocker.patch("jobbergate_cli.subapps.job_scripts.app.terminal_message")

        with mock.patch.object(pathlib.Path, "cwd", return_value=tmp_path):
            with mock.patch(
                "jobbergate_cli.subapps.job_scripts.app.download_job_script_files",
                return_value=list(f["filename"] for f in job_script_data["files"]),
            ) as mocked_save_job_script_files:
                result = cli_runner.invoke(test_app, shlex.split(f"download {cli_selector}"))

                mocked_save_job_script_files.assert_called_once_with(1, dummy_context, tmp_path)

        assert result.exit_code == 0, f"download failed: {result.stdout}"
        mocked_render.assert_called_once_with(
            "A total of 1 job script files were successfully downloaded.",
            subject="Job script download succeeded",
        )


class TestCloneJobScript:
    @pytest.mark.parametrize("selector_template", ["{id}", "-i {id}", "--id={id}", "--id {id}"])
    def test_clone__success(
        self,
        respx_mock,
        make_test_app,
        dummy_job_script_data,
        dummy_domain,
        dummy_context,
        cli_runner,
        mocker,
        selector_template,
    ):
        """
        Test that the clone application subcommand works as expected.
        """

        job_script_data = dummy_job_script_data[0]
        id = job_script_data["id"]

        cli_selector = selector_template.format(id=id)

        clone_route = respx_mock.post(f"{dummy_domain}/jobbergate/job-scripts/clone/{id}").mock(
            return_value=httpx.Response(
                httpx.codes.CREATED,
                json=job_script_data,
            ),
        )
        mocked_render = mocker.patch("jobbergate_cli.subapps.job_scripts.app.render_single_result")

        test_app = make_test_app("clone", clone)
        result = cli_runner.invoke(
            test_app,
            shlex.split(
                "clone {} --name={} --description={}".format(
                    cli_selector,
                    shlex.quote(job_script_data["name"]),
                    shlex.quote(job_script_data["description"]),
                ),
            ),
        )

        assert clone_route.called

        assert result.exit_code == 0, f"clone failed: {result.stdout}"
        mocked_render.assert_called_once_with(
            dummy_context,
            JobScriptResponse(**job_script_data),
            title="Cloned Job Script",
            hidden_fields=HIDDEN_FIELDS,
        )
