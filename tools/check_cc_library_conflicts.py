import os
import subprocess
import tempfile
from pathlib import Path

import click
from registry import RegistryClient


@click.command()
@click.argument("module")
@click.option("--version")
@click.option("--registry", default=".")
def check_cc_library_conflicts(module, version, registry):
    """
    Check if a modules cc_library targets conflict with any other modules in
    the registry.
    """
    client = RegistryClient(registry)
    if not client.contains(module):
        raise click.BadParameter(
            f"{module=} not found in {registry=}. "
            f"Possible modules: {', '.join(client.get_all_modules())}"
        )
    client.update_versions(module)
    versions = [ver for _, ver in client.get_module_versions(module)]
    version = version or versions[-1]
    if not client.contains(module, version):
        raise click.BadParameter(
            f"{version=} not found for {module=}. "
            f"Possible versions: {', '.join(versions)}"
        )
    
    # modules that require more attention to test against
    # TODO: Support these modules
    problem_modules = {"rules_rust"}

    tmp_dir = Path(tempfile.mkdtemp())
    modules = []

    with open(tmp_dir.joinpath("MODULE.bazel"), 'w') as module_bazel_file:
        for mod in client.get_all_modules():
            if mod in problem_modules:
                continue

            client.update_versions(mod)
            mod_versions = [ver for _, ver in client.get_module_versions(mod, include_yanked = False)]
            if len(mod_versions) > 0:
                modules.append(mod)
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
    main_module_targets = [target for target in cc_library_targets if target.startswith(f"@{module}//")]

    modules_with_cc_targets = set()
    for target in cc_library_targets:
        module_name = target[1:target.index("/")]
        modules_with_cc_targets.add(module_name)
    
    # re-write MODULE.bazel to get rid of modules without cc_library targets
    # this is necessary due to several errors in other moduels not allowing a
    # build to occur
    with open(tmp_dir.joinpath("MODULE.bazel"), 'w') as module_bazel_file:
        for mod in modules_with_cc_targets:
            mod_versions = [ver for _, ver in client.get_module_versions(mod, include_yanked = False)]
            mod_version = mod_versions[-1]
            module_bazel_file.write(f"bazel_dep(name=\"{mod}\", version=\"{mod_version}\")\n")

    open(tmp_dir.joinpath("dummy_main.cc"), "w").write("int main() {}\n")

    build_file_path = tmp_dir.joinpath("BUILD.bazel")
    count = 0
    with open(build_file_path, "w") as build_file:
        for main_module_target in main_module_targets:
            for target in cc_library_targets:
                if main_module_target == target:
                    continue
                build_file.write(f'# {main_module_target} + {target}\n')
                build_file.write(f'cc_binary(name="{count}", deps = ["{main_module_target}", "{target}"], srcs=["dummy_main.cc"])\n\n')
                count += 1


    print(tmp_dir)


if __name__ == "__main__":
    # Under 'bazel run' we want to run within the source folder instead of the execroot.
    if os.getenv("BUILD_WORKSPACE_DIRECTORY"):
        os.chdir(os.getenv("BUILD_WORKSPACE_DIRECTORY"))
    check_cc_library_conflicts()
