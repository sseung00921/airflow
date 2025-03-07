# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
import time
from copy import deepcopy
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import click

from airflow_breeze.params.build_ci_params import BuildCiParams
from airflow_breeze.utils.ci_group import ci_group
from airflow_breeze.utils.click_utils import BreezeGroup
from airflow_breeze.utils.common_options import (
    option_additional_airflow_extras,
    option_additional_dev_apt_command,
    option_additional_dev_apt_deps,
    option_additional_dev_apt_env,
    option_additional_pip_install_flags,
    option_additional_python_deps,
    option_airflow_constraints_location,
    option_airflow_constraints_mode_ci,
    option_airflow_constraints_reference_build,
    option_answer,
    option_build_progress,
    option_build_timeout_minutes,
    option_builder,
    option_commit_sha,
    option_debian_version,
    option_debug_resources,
    option_dev_apt_command,
    option_dev_apt_deps,
    option_docker_cache,
    option_dry_run,
    option_eager_upgrade_additional_requirements,
    option_github_repository,
    option_github_token,
    option_image_name,
    option_image_tag_for_building,
    option_image_tag_for_pulling,
    option_image_tag_for_verifying,
    option_include_success_outputs,
    option_install_providers_from_sources,
    option_parallelism,
    option_platform_multiple,
    option_prepare_buildx_cache,
    option_pull,
    option_push,
    option_python,
    option_python_image,
    option_python_versions,
    option_run_in_parallel,
    option_skip_cleanup,
    option_tag_as_latest,
    option_upgrade_on_failure,
    option_upgrade_to_newer_dependencies,
    option_verbose,
    option_verify,
    option_version_suffix_for_pypi_ci,
    option_wait_for_image,
)
from airflow_breeze.utils.confirm import STANDARD_TIMEOUT, Answer, user_confirm
from airflow_breeze.utils.console import Output, get_console
from airflow_breeze.utils.docker_command_utils import (
    build_cache,
    check_remote_ghcr_io_commands,
    make_sure_builder_configured,
    perform_environment_checks,
    prepare_docker_build_command,
    warm_up_docker_builder,
)
from airflow_breeze.utils.image import run_pull_image, run_pull_in_parallel, tag_image_as_latest
from airflow_breeze.utils.mark_image_as_refreshed import mark_image_as_refreshed
from airflow_breeze.utils.md5_build_check import md5sum_check_if_build_is_needed
from airflow_breeze.utils.parallel import DockerBuildxProgressMatcher, check_async_run_results, run_with_pool
from airflow_breeze.utils.path_utils import AIRFLOW_SOURCES_ROOT, BUILD_CACHE_DIR
from airflow_breeze.utils.python_versions import get_python_version_list
from airflow_breeze.utils.registry import login_to_github_docker_registry
from airflow_breeze.utils.run_tests import verify_an_image
from airflow_breeze.utils.run_utils import (
    fix_group_permissions,
    instruct_build_image,
    is_repo_rebased,
    run_command,
)
from airflow_breeze.utils.shared_options import get_dry_run, get_verbose

if TYPE_CHECKING:
    from airflow_breeze.params.shell_params import ShellParams


@click.group(
    cls=BreezeGroup, name="ci-image", help="Tools that developers can use to manually manage CI images"
)
def ci_image():
    pass


def check_if_image_building_is_needed(ci_image_params: BuildCiParams, output: Output | None) -> bool:
    """Starts building attempt. Returns false if we should not continue"""
    if not ci_image_params.force_build and not ci_image_params.upgrade_to_newer_dependencies:
        if not should_we_run_the_build(build_ci_params=ci_image_params):
            return False
    return True


def run_build_in_parallel(
    image_params_list: list[BuildCiParams],
    python_version_list: list[str],
    include_success_outputs: bool,
    parallelism: int,
    skip_cleanup: bool,
    debug_resources: bool,
) -> None:
    warm_up_docker_builder(image_params_list[0])
    with ci_group(f"Building for {python_version_list}"):
        all_params = [f"CI {image_params.python}" for image_params in image_params_list]
        with run_with_pool(
            parallelism=parallelism,
            all_params=all_params,
            debug_resources=debug_resources,
            progress_matcher=DockerBuildxProgressMatcher(),
        ) as (pool, outputs):
            results = [
                pool.apply_async(
                    run_build_ci_image,
                    kwds={
                        "ci_image_params": image_params,
                        "output": outputs[index],
                    },
                )
                for index, image_params in enumerate(image_params_list)
            ]
    check_async_run_results(
        results=results,
        success="All images built correctly",
        outputs=outputs,
        include_success_outputs=include_success_outputs,
        skip_cleanup=skip_cleanup,
    )


def prepare_for_building_ci_image(params: BuildCiParams):
    check_if_image_building_is_needed(params, output=None)
    make_sure_builder_configured(params=params)
    login_to_github_docker_registry(
        github_token=params.github_token,
        output=None,
    )


def build_timout_handler(build_process_group_id: int, signum, frame):
    # Kill the forked process group - it will kill the build even if it is running in parallel
    # with multiple processes and docker build sessions
    os.killpg(build_process_group_id, signal.SIGTERM)
    os.waitpid(build_process_group_id, 0)
    # give the output a little time to flush so that the helpful error message is not hidden
    time.sleep(5)
    if os.environ.get("GITHUB_ACTIONS", "false") != "true":
        get_console().print("::endgroup::")
    get_console().print()
    get_console().print(
        "[error]The build timed out. This is likely because `pip` "
        "started to backtrack dependency resolution.\n"
    )
    get_console().print(
        "[warning]Please follow the instructions in "
        "`dev/MANUALLY_GENERATING_IMAGE_CACHE_AND_CONSTRAINTS.md"
    )
    get_console().print(
        "[warning]in the `How to figure out backtracking dependencies` "
        "chapter as soon as possible. The longer it is delayed, "
        "the more difficult it will be to find the culprit.\n"
    )
    from airflow_breeze.utils.backtracking import print_backtracking_candidates

    print_backtracking_candidates()
    sys.exit(1)


def kill_process_group(build_process_group_id: int):
    try:
        os.killpg(build_process_group_id, signal.SIGTERM)
    except OSError:
        pass


def get_exitcode(status: int) -> int:
    # In Python 3.9+ we will be able to use
    # os.waitstatus_to_exitcode(status) - see https://github.com/python/cpython/issues/84275
    # but until then we need to do this ugly conversion
    if os.WIFSIGNALED(status):
        return -os.WTERMSIG(status)
    elif os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    elif os.WIFSTOPPED(status):
        return -os.WSTOPSIG(status)
    else:
        return 1


@ci_image.command(name="build")
@option_python
@option_debian_version
@option_upgrade_to_newer_dependencies
@option_upgrade_on_failure
@option_platform_multiple
@option_github_token
@option_docker_cache
@option_image_tag_for_building
@option_prepare_buildx_cache
@option_push
@option_install_providers_from_sources
@option_additional_airflow_extras
@option_additional_dev_apt_deps
@option_additional_python_deps
@option_additional_dev_apt_command
@option_additional_dev_apt_env
@option_builder
@option_build_progress
@option_commit_sha
@option_dev_apt_command
@option_dev_apt_deps
@option_python_image
@option_eager_upgrade_additional_requirements
@option_airflow_constraints_location
@option_airflow_constraints_mode_ci
@option_airflow_constraints_reference_build
@option_tag_as_latest
@option_additional_pip_install_flags
@option_github_repository
@option_version_suffix_for_pypi_ci
@option_build_timeout_minutes
@option_run_in_parallel
@option_parallelism
@option_skip_cleanup
@option_debug_resources
@option_include_success_outputs
@option_python_versions
@option_verbose
@option_dry_run
@option_answer
def build(
    # Build options
    python: str,
    debian_version: str,
    upgrade_to_newer_dependencies: bool,
    upgrade_on_failure: bool,
    platform: str | None,
    github_token: str | None,
    docker_cache: str,
    image_tag: str,
    prepare_buildx_cache: bool,
    push: bool,
    install_providers_from_sources: bool,
    additional_airflow_extras: str | None,
    additional_dev_apt_deps: str | None,
    additional_python_deps: str | None,
    additional_dev_apt_command: str | None,
    additional_dev_apt_env: str | None,
    builder: str,
    build_progress: str,
    commit_sha: str | None,
    dev_apt_command: str | None,
    dev_apt_deps: str | None,
    eager_upgrade_additional_requirements: str | None,
    airflow_constraints_location: str | None,
    airflow_constraints_mode: str,
    airflow_constraints_reference: str,
    tag_as_latest: bool,
    additional_pip_install_flags: str | None,
    github_repository: str,
    python_image: str | None,
    version_suffix_for_pypi: str,
    # Parallel building
    run_in_parallel: bool,
    parallelism: int,
    skip_cleanup: bool,
    debug_resources: bool,
    include_success_outputs,
    python_versions: str,
    # Other options
    build_timeout_minutes: int | None,
):
    """Build CI image. Include building multiple images for all python versions."""

    def run_build(ci_image_params: BuildCiParams) -> None:
        return_code, info = run_build_ci_image(
            ci_image_params=ci_image_params,
            output=None,
        )
        if return_code != 0:
            get_console().print(f"[error]Error when building image! {info}")
            sys.exit(return_code)

    if build_timeout_minutes:
        pid = os.fork()
        if pid:
            # Parent process - send signal to process group of the child process
            handler: Callable[..., tuple[Any, Any]] = partial(build_timout_handler, pid)
            # kill the child process group when we exit before - for example when we are Ctrl-C-ed
            atexit.register(kill_process_group, pid)
            signal.signal(signal.SIGALRM, handler)
            signal.alarm(build_timeout_minutes * 60)
            child_pid, status = os.waitpid(pid, 0)
            exit_code = get_exitcode(status)
            if exit_code:
                get_console().print(f"[error]Exiting with exit code {exit_code}")
            else:
                get_console().print(f"[success]Exiting with exit code {exit_code}")
            sys.exit(exit_code)
        else:
            # turn us into a process group leader
            os.setpgid(0, 0)

    perform_environment_checks()
    check_remote_ghcr_io_commands()
    fix_group_permissions()
    base_build_params = BuildCiParams(
        force_build=True,
        python=python,
        debian_version=debian_version,
        upgrade_to_newer_dependencies=upgrade_to_newer_dependencies,
        upgrade_on_failure=upgrade_on_failure,
        github_token=github_token,
        docker_cache=docker_cache,
        image_tag=image_tag,
        prepare_buildx_cache=prepare_buildx_cache,
        push=push,
        install_providers_from_sources=install_providers_from_sources,
        additional_airflow_extras=additional_airflow_extras,
        additional_python_deps=additional_python_deps,
        additional_dev_apt_command=additional_dev_apt_command,
        additional_dev_apt_env=additional_dev_apt_env,
        builder=builder,
        build_progress=build_progress,
        commit_sha=commit_sha,
        dev_apt_command=dev_apt_command,
        dev_apt_deps=dev_apt_deps,
        eager_upgrade_additional_requirements=eager_upgrade_additional_requirements,
        airflow_constraints_location=airflow_constraints_location,
        airflow_constraints_mode=airflow_constraints_mode,
        airflow_constraints_reference=airflow_constraints_reference,
        tag_as_latest=tag_as_latest,
        additional_pip_install_flags=additional_pip_install_flags,
        github_repository=github_repository,
        python_image=python_image,
        version_suffix_for_pypi=version_suffix_for_pypi,
    )
    if platform:
        base_build_params.platform = platform
    if additional_dev_apt_deps:
        # For CI image we only set additional_dev_apt_deps when we explicitly pass it
        base_build_params.additional_dev_apt_deps = additional_dev_apt_deps

    if run_in_parallel:
        python_version_list = get_python_version_list(python_versions)
        params_list: list[BuildCiParams] = []
        for python in python_version_list:
            build_params = deepcopy(base_build_params)
            build_params.python = python
            params_list.append(build_params)
        prepare_for_building_ci_image(params=params_list[0])
        run_build_in_parallel(
            image_params_list=params_list,
            python_version_list=python_version_list,
            include_success_outputs=include_success_outputs,
            parallelism=parallelism,
            skip_cleanup=skip_cleanup,
            debug_resources=debug_resources,
        )
    else:
        prepare_for_building_ci_image(params=base_build_params)
        run_build(ci_image_params=base_build_params)


@ci_image.command(name="pull")
@option_python
@option_run_in_parallel
@option_parallelism
@option_skip_cleanup
@option_debug_resources
@option_include_success_outputs
@option_python_versions
@option_github_token
@option_verify
@option_wait_for_image
@option_image_tag_for_pulling
@option_tag_as_latest
@option_github_repository
@option_verbose
@option_dry_run
@click.argument("extra-pytest-args", nargs=-1, type=click.UNPROCESSED)
def pull(
    python: str,
    run_in_parallel: bool,
    python_versions: str,
    github_token: str,
    parallelism: int,
    skip_cleanup: bool,
    debug_resources: bool,
    include_success_outputs: bool,
    image_tag: str,
    wait_for_image: bool,
    tag_as_latest: bool,
    verify: bool,
    github_repository: str,
    extra_pytest_args: tuple,
):
    """Pull and optionally verify CI images - possibly in parallel for all Python versions."""
    perform_environment_checks()
    check_remote_ghcr_io_commands()
    login_to_github_docker_registry(
        github_token=github_token,
        output=None,
    )
    if run_in_parallel:
        python_version_list = get_python_version_list(python_versions)
        ci_image_params_list = [
            BuildCiParams(
                image_tag=image_tag,
                python=python,
                github_repository=github_repository,
                github_token=github_token,
            )
            for python in python_version_list
        ]
        run_pull_in_parallel(
            parallelism=parallelism,
            skip_cleanup=skip_cleanup,
            debug_resources=debug_resources,
            include_success_outputs=include_success_outputs,
            image_params_list=ci_image_params_list,
            python_version_list=python_version_list,
            verify=verify,
            wait_for_image=wait_for_image,
            tag_as_latest=tag_as_latest,
            extra_pytest_args=extra_pytest_args if extra_pytest_args is not None else (),
        )
    else:
        image_params = BuildCiParams(
            image_tag=image_tag,
            python=python,
            github_repository=github_repository,
            github_token=github_token,
        )
        return_code, info = run_pull_image(
            image_params=image_params,
            output=None,
            wait_for_image=wait_for_image,
            tag_as_latest=tag_as_latest,
        )
        if return_code != 0:
            get_console().print(f"[error]There was an error when pulling CI image: {info}[/]")
            sys.exit(return_code)


@ci_image.command(
    name="verify",
    context_settings=dict(
        ignore_unknown_options=True,
        allow_extra_args=True,
    ),
)
@option_python
@option_github_repository
@option_image_tag_for_verifying
@option_image_name
@option_pull
@option_github_token
@option_verbose
@option_dry_run
@click.argument("extra_pytest_args", nargs=-1, type=click.UNPROCESSED)
def verify(
    python: str,
    image_name: str,
    image_tag: str | None,
    pull: bool,
    github_token: str,
    github_repository: str,
    extra_pytest_args: tuple,
):
    """Verify CI image."""
    perform_environment_checks()
    login_to_github_docker_registry(
        github_token=github_token,
        output=None,
    )
    if image_name is None:
        build_params = BuildCiParams(
            python=python,
            image_tag=image_tag,
            github_repository=github_repository,
            github_token=github_token,
        )
        image_name = build_params.airflow_image_name_with_tag
    if pull:
        check_remote_ghcr_io_commands()
        command_to_run = ["docker", "pull", image_name]
        run_command(command_to_run, check=True)
    get_console().print(f"[info]Verifying CI image: {image_name}[/]")
    return_code, info = verify_an_image(
        image_name=image_name,
        output=None,
        image_type="CI",
        slim_image=False,
        extra_pytest_args=extra_pytest_args,
    )
    sys.exit(return_code)


def should_we_run_the_build(build_ci_params: BuildCiParams) -> bool:
    """
    Check if we should run the build based on what files have been modified since last build and answer from
    the user.

    * If build is needed, the user is asked for confirmation
    * If the branch is not rebased it warns the user to rebase (to make sure latest remote cache is useful)
    * Builds Image/Skips/Quits depending on the answer

    :param build_ci_params: parameters for the build
    """
    # We import those locally so that click autocomplete works
    from inputimeout import TimeoutOccurred

    if not md5sum_check_if_build_is_needed(
        md5sum_cache_dir=build_ci_params.md5sum_cache_dir,
        skip_provider_dependencies_check=build_ci_params.skip_provider_dependencies_check,
    ):
        return False
    try:
        answer = user_confirm(
            message="Do you want to build the image (this works best when you have good connection and "
            "can take usually from 20 seconds to few minutes depending how old your image is)?",
            timeout=STANDARD_TIMEOUT,
            default_answer=Answer.NO,
        )
        if answer == answer.YES:
            if is_repo_rebased(build_ci_params.github_repository, build_ci_params.airflow_branch):
                return True
            else:
                get_console().print(
                    "\n[warning]This might take a lot of time (more than 10 minutes) even if you have "
                    "a good network connection. We think you should attempt to rebase first.[/]\n"
                )
                answer = user_confirm(
                    "But if you really, really want - you can attempt it. Are you really sure?",
                    timeout=STANDARD_TIMEOUT,
                    default_answer=Answer.NO,
                )
                if answer == Answer.YES:
                    return True
                else:
                    get_console().print(
                        f"[info]Please rebase your code to latest {build_ci_params.airflow_branch} "
                        "before continuing.[/]\nCheck this link to find out how "
                        "https://github.com/apache/airflow/blob/main/CONTRIBUTING.rst#id15\n"
                    )
                    get_console().print("[error]Exiting the process[/]\n")
                    sys.exit(1)
        elif answer == Answer.NO:
            instruct_build_image(build_ci_params.python)
            return False
        else:  # users_status == Answer.QUIT:
            get_console().print("\n[warning]Quitting the process[/]\n")
            sys.exit()
    except TimeoutOccurred:
        get_console().print("\nTimeout. Considering your response as No\n")
        instruct_build_image(build_ci_params.python)
        return False
    except Exception as e:
        get_console().print(f"\nTerminating the process on {e}")
        sys.exit(1)


def run_build_ci_image(
    ci_image_params: BuildCiParams,
    output: Output | None,
) -> tuple[int, str]:
    """
    Builds CI image:

      * fixes group permissions for files (to improve caching when umask is 002)
      * converts all the parameters received via kwargs into BuildCIParams (including cache)
      * prints info about the image to build
      * logs int to docker registry on CI if build cache is being executed
      * removes "tag" for previously build image so that inline cache uses only remote image
      * constructs docker-compose command to run based on parameters passed
      * run the build command
      * update cached information that the build completed and saves checksums of all files
        for quick future check if the build is needed

    :param ci_image_params: CI image parameters
    :param output: output redirection
    """
    if (
        ci_image_params.is_multi_platform()
        and not ci_image_params.push
        and not ci_image_params.prepare_buildx_cache
    ):
        get_console(output=output).print(
            "\n[red]You cannot use multi-platform build without using --push flag or "
            "preparing buildx cache![/]\n"
        )
        return 1, "Error: building multi-platform image without --push."
    if get_verbose() or get_dry_run():
        get_console(output=output).print(
            f"\n[info]Building CI image of airflow from {AIRFLOW_SOURCES_ROOT} "
            f"python version: {ci_image_params.python}[/]\n"
        )
    if ci_image_params.prepare_buildx_cache:
        build_command_result = build_cache(
            image_params=ci_image_params,
            output=output,
        )
    else:
        env = os.environ.copy()
        env["DOCKER_BUILDKIT"] = "1"
        subprocess.run(
            [
                sys.executable,
                os.fspath(
                    AIRFLOW_SOURCES_ROOT
                    / "scripts"
                    / "ci"
                    / "pre_commit"
                    / "pre_commit_update_providers_dependencies.py"
                ),
            ],
            check=False,
        )
        get_console(output=output).print(f"\n[info]Building CI Image for Python {ci_image_params.python}\n")
        build_command_result = run_command(
            prepare_docker_build_command(
                image_params=ci_image_params,
            ),
            cwd=AIRFLOW_SOURCES_ROOT,
            text=True,
            check=False,
            env=env,
            output=output,
        )
        if build_command_result.returncode != 0 and not ci_image_params.upgrade_to_newer_dependencies:
            if ci_image_params.upgrade_on_failure:
                ci_image_params.upgrade_to_newer_dependencies = True
                get_console().print(
                    "[warning]Attempting to build with upgrade_to_newer_dependencies on failure"
                )
                build_command_result = run_command(
                    prepare_docker_build_command(
                        image_params=ci_image_params,
                    ),
                    cwd=AIRFLOW_SOURCES_ROOT,
                    env=env,
                    text=True,
                    check=False,
                    output=output,
                )
            else:
                get_console().print(
                    "[warning]Your image build failed. It could be caused by conflicting dependencies."
                )
                get_console().print(
                    "[info]Run `breeze ci-image build --upgrade-to-newer-dependencies` to upgrade them.\n"
                )
        if build_command_result.returncode == 0:
            if ci_image_params.tag_as_latest:
                build_command_result = tag_image_as_latest(image_params=ci_image_params, output=output)
            if ci_image_params.preparing_latest_image():
                if get_dry_run():
                    get_console(output=output).print(
                        "[info]Not updating build hash because we are in `dry_run` mode.[/]"
                    )
                else:
                    mark_image_as_refreshed(ci_image_params)
    return build_command_result.returncode, f"Image build: {ci_image_params.python}"


def rebuild_or_pull_ci_image_if_needed(command_params: ShellParams | BuildCiParams) -> None:
    """
    Rebuilds CI image if needed and user confirms it.

    :param command_params: parameters of the command to execute
    """
    build_ci_image_check_cache = Path(
        BUILD_CACHE_DIR, command_params.airflow_branch, f".built_{command_params.python}"
    )
    ci_image_params = BuildCiParams(
        python=command_params.python,
        builder=command_params.builder,
        github_repository=command_params.github_repository,
        upgrade_to_newer_dependencies=False,
        image_tag=command_params.image_tag,
        platform=command_params.platform,
        force_build=command_params.force_build,
        skip_provider_dependencies_check=command_params.skip_provider_dependencies_check,
    )
    if command_params.image_tag is not None and command_params.image_tag != "latest":
        return_code, message = run_pull_image(
            image_params=ci_image_params,
            output=None,
            wait_for_image=True,
            tag_as_latest=False,
        )
        if return_code != 0:
            get_console().print(f"[error]Pulling image with {command_params.image_tag} failed! {message}[/]")
            sys.exit(return_code)
        return
    if build_ci_image_check_cache.exists():
        if get_verbose():
            get_console().print(f"[info]{command_params.image_type} image already built locally.[/]")
    else:
        get_console().print(
            f"[warning]{command_params.image_type} image for Python {command_params.python} "
            f"was never built locally or was deleted. Forcing build.[/]"
        )
        ci_image_params.force_build = True
    if check_if_image_building_is_needed(
        ci_image_params=ci_image_params,
        output=None,
    ):
        run_build_ci_image(ci_image_params=ci_image_params, output=None)
