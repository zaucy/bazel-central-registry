import os
import subprocess
import tempfile
import itertools
import sys
import argparse
import json
import shutil
from enum import Enum
from pathlib import Path

from registry import RegistryClient

RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
RESET = "\x1b[0m"

class TargetBuildStatus(Enum):
    NONE = 0
    SUCCESS = 1
    FAILURE = 2

COLOR = {
  TargetBuildStatus.NONE: "",
  TargetBuildStatus.SUCCESS: GREEN,
  TargetBuildStatus.FAILURE: RED,
}

_reports = []
def report(msg):
    _reports.append(msg)

def print_reports():
    for report in _reports:
        print(report)

class TargetBuildResult:
    description = ""
    status = TargetBuildStatus.NONE

    def __init__(self, description):
        self.description = description

    def report_result(self):
        report(f"{COLOR[self.status]}{self.status}\t\t{self.description}{RESET}")

def create_check_headers_dir(tmp_dir):
    check_dir = tmp_dir.joinpath("_check")
    os.mkdir(check_dir)

def clean_check_headers_dir(tmp_dir):
    check_dir = tmp_dir.joinpath("_check")
    shutil.rmtree(check_dir)

def check_target_headers_pass(target, headers, tmp_dir):
    check_dir = tmp_dir.joinpath("_check")
    build_file_path = check_dir.joinpath("BUILD.bazel")
    check_source_path = check_dir.joinpath("check.cc")

    with open(build_file_path, "w") as build_file:
        build_file.write(f'cc_binary(name="_check", deps=["{target}"], srcs=["check.cc"])\n')
    
    with open(check_source_path, "w") as check_source:
        for header in headers:
            check_source.write(f"#include {header}\n");
        check_source.write("\nint main() {}\n")

    result = subprocess.run(
        ["bazel", "build", "//_check"],
        cwd = tmp_dir,
    )

    return result.returncode == 0


def get_module_headers(target, tmp_dir):
    gen_source_starlark_path = Path(os.getcwd()).joinpath("tools").joinpath("gen_cc_library_test_source.cquery")
    result = subprocess.run(
        ["bazel", "cquery", target, "--output=starlark", f"--starlark:file={gen_source_starlark_path}"],
        capture_output = True,
        text = True,
        cwd = tmp_dir,
    )

    headers = [line.strip() for line in result.stdout.split("\n") if line.strip()]
    if check_target_headers_pass(target, headers, tmp_dir):
        return headers

    for n in range(len(headers), 1, -1):
        for header_combos in itertools.combinations(headers, n):
            header_combos_list = list(header_combos)
            if check_target_headers_pass(target, header_combos_list, tmp_dir):
                report(f"{YELLOW}SubsetHeadersWarning\t\tOnly {len(header_combos_list)}/{len(headers)} headers built successfully for {target}{RESET}")
                for failed_header in set(headers).difference(set(header_combos)):
                    report(f"{YELLOW} - {failed_header}{RESET}")
                return header_combos_list

    return []


def check_cc_library_conflicts(modules, registry, max_combinations):
    """
    Check if a modules cc_library targets conflict with other modules in the
    registry.
    """
    client = RegistryClient(registry)
    for module in modules:
        if not client.contains(module):
            raise click.BadParameter(
                f"{module=} not found in {registry=}. "
                f"Possible modules: {', '.join(client.get_all_modules())}"
            )

        mod_versions = [ver for _, ver in client.get_module_versions(module, include_yanked = False)]
        if len(mod_versions) == 0:
            raise click.BadParameter(
                f"{module=} only contains yanked versions"
            )

    tmp_dir = Path(tempfile.mkdtemp())
    gen_source_starlark_path = Path(os.getcwd()).joinpath("tools").joinpath("gen_cc_library_test_source.cquery")

    with open(tmp_dir.joinpath("MODULE.bazel"), 'w') as module_bazel_file:
        for mod in modules:
            mod_versions = [ver for _, ver in client.get_module_versions(mod, include_yanked = False)]
            mod_version = mod_versions[-1]
            module_bazel_file.write(f"bazel_dep(name=\"{mod}\", version=\"{mod_version}\")\n")

    query_string = " + ".join([f"kind(cc_library, attr(visibility, \"//visibility:public\", @{mod}//...))" for mod in modules])

    result = subprocess.run(
        ["bazel", "query", query_string, "-k"],
        capture_output = True,
        text = True,
        cwd = tmp_dir,
    )
    cc_library_targets = [line for line in result.stdout.split('\n') if line]
    cc_library_target_headers = dict()

    build_file_path = tmp_dir.joinpath("BUILD.bazel")
    count = 0
    target_build_results = dict()


    create_check_headers_dir(tmp_dir)
    for target in cc_library_targets:
        cc_library_target_headers[target] = get_module_headers(target, tmp_dir)
    clean_check_headers_dir(tmp_dir)

    with open(build_file_path, "w") as build_file:
        for combos in itertools.combinations(cc_library_targets, max_combinations):
            target_name = f"{count}"
            description = " + ".join(combos)
            deps = ", ".join(f'"{t}"' for t in combos)
            target_build_results[f"//:{target_name}"] = TargetBuildResult(" + ".join(combos))
            generated_source_path = tmp_dir.joinpath(f"{target_name}.cc")

            with open(generated_source_path, "w") as generated_source:
                for target in combos:
                    generated_source.write(f"// {target} headers\n")
                    for header in cc_library_target_headers[target]:
                        generated_source.write(f"#include {header}\n")
                    generated_source.write("\n")

                generated_source.write("\nint main() {}\n")

            build_file.write(f"# {description}\n")
            build_file.write(f'cc_binary(name="{target_name}", deps=[{deps}], srcs=["{target_name}.cc"])\n\n')
            count += 1

    subprocess.run(
        ["bazel", "build", "//...", "-k", "--build_event_json_file=build.json"],
        cwd = tmp_dir,
    )

    with open(tmp_dir.joinpath("build.json"), "r") as build_json_file:
        while line := build_json_file.readline().rstrip():
            msg = json.loads(line)
            if "targetCompleted" in msg["id"]:
                label = msg["id"]["targetCompleted"]["label"]
                payload = msg["completed"]
                success = False
                if "success" in payload:
                    success = payload["success"]

                target_build_results[label].status = TargetBuildStatus.SUCCESS if success else TargetBuildStatus.FAILURE

    for label in target_build_results:
        result = target_build_results[label]
        result.report_result()

    print_reports()

    subprocess.run(
        ["bazel", "shutdown"],
        cwd = tmp_dir,
    )

    shutil.rmtree(tmp_dir)

if __name__ == "__main__":
    # Under 'bazel run' we want to run within the source folder instead of the execroot.
    if os.getenv("BUILD_WORKSPACE_DIRECTORY"):
        os.chdir(os.getenv("BUILD_WORKSPACE_DIRECTORY"))

    parser = argparse.ArgumentParser(
        prog = 'check_cc_library_conflicts',
        description = 'Check if a modules cc_library targets conflict with other modules in the registry.'
    )

    parser.add_argument('modules', nargs = '+')
    parser.add_argument('--registry', default = '.')
    parser.add_argument('--max_combinations', type=int)

    args = parser.parse_args()

    if not args.max_combinations:
        args.max_combinations = len(args.modules)
    
    check_cc_library_conflicts(args.modules, args.registry, args.max_combinations)
