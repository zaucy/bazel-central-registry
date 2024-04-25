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

class TargetBuildResult:
    description = ""
    status = TargetBuildStatus.NONE

    def __init__(self, description):
        self.description = description

    def print_result(self):
        print(f"{COLOR[self.status]}{self.status}\t\t{self.description}{RESET}")

def check_cc_library_conflicts(modules, registry):
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

    open(tmp_dir.joinpath("dummy_main.cc"), "w").write("int main() {}\n")

    build_file_path = tmp_dir.joinpath("BUILD.bazel")
    count = 0
    target_build_results = dict()
    with open(build_file_path, "w") as build_file:
        for (a, b) in itertools.combinations(cc_library_targets, 2):
            target_name = f"{count}"
            target_build_results[f"//:{target_name}"] = TargetBuildResult(f"{a} + {b}")
            build_file.write(f'# {a} + {b}\n')
            build_file.write(f'cc_binary(name="{target_name}", deps=["{a}", "{b}"], srcs=["dummy_main.cc"])\n\n')
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
        target_build_results[label].print_result()

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

    args = parser.parse_args()
    
    check_cc_library_conflicts(args.modules, args.registry)
