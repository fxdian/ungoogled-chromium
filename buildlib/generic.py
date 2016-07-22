# ungoogled-chromium: Google Chromium patches for removing Google integration, enhancing privacy, and adding features
# Copyright (C) 2016  Eloston
#
# This file is part of ungoogled-chromium.
#
# ungoogled-chromium is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ungoogled-chromium is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with ungoogled-chromium.  If not, see <http://www.gnu.org/licenses/>.

'''Code for use across platforms'''

import tarfile
import urllib.request
import hashlib
import pathlib
import shutil
import re
import subprocess
import logging
import configparser
import distutils.dir_util

class GenericPlatform:
    # Define default paths and file names. Some may be overridden by the methods
    COMMON_RESOURCES = pathlib.Path("resources", "common")
    PLATFORM_RESOURCES = None
    CLEANING_LIST = pathlib.Path("cleaning_list")
    DOMAIN_REGEX_LIST = pathlib.Path("domain_regex_list")
    DOMAIN_SUBSTITUTION_LIST = pathlib.Path("domain_substitution_list")
    PATCHES = pathlib.Path("patches")
    PATCH_ORDER = pathlib.Path("patch_order")
    GYP_FLAGS = pathlib.Path("gyp_flags")
    GN_ARGS = pathlib.Path("gn_args.ini")
    UNGOOGLED_DIR = pathlib.Path(".ungoogled")
    SANDBOX_ROOT = pathlib.Path("build_sandbox")
    BUILD_OUTPUT = pathlib.Path("out", "Release") # Change this for GN

    def __init__(self, version, revision, logger=None, sandbox_root=None):
        self.version = version
        self.revision = revision

        if logger is None:
            logger = logging.getLogger("ungoogled_chromium")
            logger.setLevel(logging.DEBUG)

            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.DEBUG)

            formatter = logging.Formatter("%(asctime)s - %(levelname)s: %(message)s")
            console_handler.setFormatter(formatter)

            logger.addHandler(console_handler)
        self.logger = logger

        if sandbox_root is None:
            self.sandbox_root = self.SANDBOX_ROOT
        else:
            self.sandbox_root = sandbox_root
        if self.sandbox_root.exists():
            if not self.sandbox_root.is_dir():
                raise Exception("sandbox_root exists, but is not a directory")
        else:
            self.logger.info("Sandbox root does not exist. Creating...")
            self.sandbox_root.mkdir()

        self.ungoogled_dir = self.sandbox_root / self.UNGOOGLED_DIR
        if self.ungoogled_dir.exists():
            if not self.ungoogled_dir.is_dir():
                raise Exception("ungoogled_dir exists, but is not a directory")
        else:
            self.logger.info("ungoogled_dir does not exist. Creating...")
            self.ungoogled_dir.mkdir()

        self.sourcearchive = None
        self.sourcearchive_hashes = None
        self.gn_command = None
        self.ninja_command = None
        self.build_output = None

        self._ran_domain_substitution = False
        self._domain_regex_cache = None

    def _read_list_resource(self, file_name, is_binary=False):
        if is_binary:
            file_mode = "rb"
        else:
            file_mode = "r"
        common_path = self.COMMON_RESOURCES / file_name
        with common_path.open(file_mode) as f:
            tmp_list = f.read().splitlines()
        if not self.PLATFORM_RESOURCES is None:
            platform_path = self.PLATFORM_RESOURCES / file_name
            if platform_path.exists():
                with platform_path.open(file_mode) as f:
                    tmp_list.extend(f.read().splitlines())
                    self.logger.debug("Successfully appended platform list")
        return [x for x in tmp_list if len(x) > 0]

    def _check_source_archive(self):
        '''
        Runs integrity checks on the source archive
        '''
        with self.sourcearchive_hashes.open("r") as hashes_file:
            for hash_line in hashes_file.read().split("\n"):
                hash_line = hash_line.split("  ")
                if hash_line[0] in hashlib.algorithms_available:
                    self.logger.info("Running '{}' hash check...".format(hash_line[0]))
                    hasher = hashlib.new(hash_line[0])
                    with self.sourcearchive.open("rb") as f:
                        hasher.update(f.read())
                        if hasher.hexdigest() == hash_line[1]:
                            self.logger.debug("'{}' hash matches".format(hash_line[0]))
                        else:
                            self.logger.error("Archive does not have matching '{algorithm}' hash '{hashhex}'".format(algorithm=hash_line[0], hashhex=hash_line[1]))
                            return None
                else:
                    self.logger.warning("Hash algorithm '{}' not available. Skipping...".format(hash_line[0]))

    def _download_source_archive(self):
        '''
        Downloads the original Chromium source code in .tar.xz format
        '''
        download_url = "https://commondatastorage.googleapis.com/chromium-browser-official/chromium-{version}.tar.xz".format(version=self.version)
        with urllib.request.urlopen(download_url) as response:
            with self.sourcearchive.open("wb") as f:
                shutil.copyfileobj(response, f)

    def _download_source_hashes(self):
        hashes_url = "https://commondatastorage.googleapis.com/chromium-browser-official/chromium-{version}.tar.xz.hashes".format(version=self.version)
        with urllib.request.urlopen(hashes_url) as response:
            with self.sourcearchive_hashes.open("wb") as f:
                shutil.copyfileobj(response, f)

    def _extract_source_archive(self, cleaning_list):
        '''
        Extract the archive located on archive_path to the sandbox root
        Also modifies cleaning_list to contain paths not removed
        '''
        class NoAppendList(list): # Hack to workaround memory issues with large tar files
            def append(self, obj):
                pass

        with tarfile.open(str(self.sourcearchive)) as tar_file_obj:
            tar_file_obj.members = NoAppendList()
            for tarinfo in tar_file_obj:
                try:
                    relative_path = pathlib.PurePosixPath(tarinfo.name).relative_to("chromium-{}".format(self.version))
                    if str(relative_path) in cleaning_list:
                        cleaning_list.remove(str(relative_path))
                    else:
                        destination = self.sandbox_root / pathlib.Path(*relative_path.parts)
                        tar_file_obj._extract_member(tarinfo, str(destination))
                except Exception as e:
                    self.logger.error("Exception thrown for tar member {}".format(tarinfo.name))
                    raise e

    def _get_parsed_domain_regexes(self):
        if self._domain_regex_cache is None:
            self._domain_regex_cache = list()
            for expression in self._read_list_resource(self.DOMAIN_REGEX_LIST, is_binary=True):
                expression = expression.split(b'#')
                self._domain_regex_cache.append((re.compile(expression[0]), expression[1]))
        return self._domain_regex_cache

    def _domain_substitute(self, regex_list, file_list, log_warnings=True):
        '''
        Runs domain substitution with regex_list over files file_list
        '''
        for path in file_list:
            try:
                with path.open(mode="r+b") as f:
                    content = f.read()
                    file_subs = 0
                    for regex_pair in regex_list:
                        compiled_regex, replacement_regex = regex_pair
                        content, number_of_subs = compiled_regex.subn(replacement_regex, content)
                        file_subs += number_of_subs
                    if file_subs > 0:
                        f.seek(0)
                        f.write(content)
                        f.truncate()
                    elif log_warnings:
                        self.logger.warning("File {} has no matches".format(path))
            except Exception as e:
                self.logger.error("Exception thrown for path {}".format(path))
                raise e

    def _generate_patches(self, output_dir, run_domain_substitution):
        platform_patch_order = self.PLATFORM_RESOURCES / self.PATCHES / self.PATCH_ORDER
        platform_patches_exist = (not self.PLATFORM_RESOURCES is None) and platform_patch_order.exists()
        with (self.COMMON_RESOURCES / self.PATCHES / self.PATCH_ORDER).open() as f:
            new_patch_order = f.read()
        if platform_patches_exist:
            self.logger.debug("Using platform patches")
            with platform_patch_order.open() as f:
                new_patch_order += f.read()

        distutils.dir_util.copy_tree(str(self.COMMON_RESOURCES / self.PATCHES), str(output_dir))
        (output_dir / self.PATCH_ORDER).unlink()
        distutils.dir_util.copy_tree(str(self.PLATFORM_RESOURCES / self.PATCHES), str(output_dir))
        (output_dir / self.PATCH_ORDER).unlink()
        with (output_dir / self.PATCH_ORDER).open("w") as f:
            f.write(new_patch_order)

        if run_domain_substitution:
            self.logger.debug("Running domain substitution over patches...")
            self._domain_substitute(self._get_parsed_domain_regexes(), self.sandbox_patches.rglob("*.patch"), log_warnings=False)

    def _gyp_generate_ninja(self, args_list, build_output, python2_command):
        command_list = list()
        if not python2_command is None:
            command_list.append(python2_command)
        command_list.append(str(pathlib.Path("build", "gyp_chromium")))
        command_list += ["--depth=.", "--check"]
        for i in args_list:
            command_list.append("-D{}".format(i))
        self.logger.debug("GYP command: {}".format(" ".join(command_list)))
        result = subprocess.run(command_list, cwd=str(self.sandbox_root))
        if not result.returncode == 0:
            raise Exception("GYP command returned non-zero exit code: {}".format(result.returncode))

    def _gn_write_args(self, args_map, build_output):
        '''
        `args_map` can be any object supporting the mapping interface
        '''
        gn_imports = list()
        gn_flags = list()
        for gn_path in args_map:
            if not gn_path == "DEFAULT" and not gn_path == "global": # Checking against DEFAULT for configparser mapping interface
                if not gn_path.lower().endswith(".gn"):
                    gn_imports.append('import("{}")'.format(gn_path))
            for flag in args_map[gn_path]:
                gn_flags.append("{}={}".format(flag, args_map[gn_path][flag]))
        with (self.sandbox_root / build_output / pathlib.Path("args.gn")).open("w") as f:
            f.write("\n".join(gn_imports))
            f.write("\n")
            f.write("\n".join(gn_flags))

    def _gn_generate_ninja(self, build_output, gn_override=None):
        command_list = list()
        if gn_override is None:
            command_list.append(self.gn_command)
        else:
            command_list.append(gn_override)
        command_list.append("gen")
        command_list.append(str(build_output))
        result = subprocess.run(command_list, cwd=str(self.sandbox_root))
        if not result.returncode == 0:
            raise Exception("gn gen returned non-zero exit code: {}".format(result.returncode))

    def _run_ninja(self, build_output, targets):
        result = subprocess.run([self.ninja_command, "-C", str(build_output), *targets], cwd=str(self.sandbox_root))
        if not result.returncode == 0:
            raise Exception("ninja returned non-zero exit code: {}".format(result.returncode))

    def _build_gn(self, python2_command):
        '''
        Build the GN tool to out/gn_tool in the build sandbox. Returns the gn command string. Only works on Linux or Mac.
        '''
        self.logger.info("Building gn...")
        temp_gn_executable = pathlib.Path("out", "temp_gn")
        if (self.sandbox_root / temp_gn_executable).exists():
            self.logger.info("Bootstrap gn already exists")
        else:
            self.logger.info("Building bootstrap gn")
            command_list = [str(pathlib.Path("tools", "gn", "bootstrap", "bootstrap.py")), "-v", "-s", "-o", str(temp_gn_executable), "--gn-gen-args= use_sysroot=false"]
            if not python2_command is None:
                command_list.insert(0, python2_command)
            result = subprocess.run(command_list, cwd=str(self.sandbox_root))
            if not result.returncode == 0:
                raise Exception("GN bootstrap command returned non-zero exit code: {}".format(result.returncode))
        self.logger.info("Building gn using bootstrap gn...")
        build_output = pathlib.Path("out", "gn_release")
        (self.sandbox_root / build_output).mkdir(parents=True, exist_ok=True)
        self._gn_write_args({"global": {"use_sysroot": "false", "is_debug": "false"}}, build_output)
        self._gn_generate_ninja(build_output, gn_override=str(temp_gn_executable))
        self._run_ninja(build_output, ["gn"])
        return str(build_output / pathlib.Path("gn"))

    def setup_chromium_source(self, check_if_exists=True, force_download=False, check_integrity=True, extract_archive=True, destination_dir=pathlib.Path("."), use_cleaning_list=True, archive_path=None, hashes_path=None):
        '''
        Sets up the Chromium source code in the build sandbox. It can download the source code in .tar.xz format, integrity check it, and extract it into the build sandbox while excluding any files in the cleaning list.

        If `check_if_exists` is True, then the source archive or its hashes file will be downloaded if they are not found. Otherwise, they will not be downloaded.
        If `force_download` is True, then the source archive will be downloaded regardless of its existence. This overrides `check_if_exists`.
        If `check_integrity` is True, then the source archive will be integrity checked. Otherwise no hashes file will be downloaded and no integrity checking is done.
        If `extract_archive` is True, then the source archive will be extracted into the build sandbox.
        `destination_dir` specifies the directory for downloading the source archive and the hashes file to.
        `use_cleaning_list` specifies whether to use the cleaning lists or not (common and platform)
        If `archive_path` is set, it must be a pathlib path instance that specifies the location to an existing source archive. It will cause the skipping of downloading the the source archive. It must be set alongside `hashes_path`.
        `hashes_path` must be a pathlib path that points to the hashes file. It will be ignored if `archive_path` is set to None.
        '''
        if archive_path is None:
            if check_if_exists and force_download:
                raise Exception("Conflicting arguments: check_if_exists and force_download")

            self.sourcearchive = destination_dir / pathlib.Path("chromium-{version}.tar.xz".format(version=self.version))
            self.sourcearchive_hashes = destination_dir / pathlib.Path("chromium-{version}.tar.xz.hashes".format(version=self.version))

            if self.sourcearchive.exists() and not self.sourcearchive.is_file():
                raise Exception("sourcearchive is an existing non-file")
            elif force_download or check_if_exists and not self.sourcearchive.is_file():
                self.logger.info("Downloading source archive...")
                self._download_source_archive()
            else:
                self.logger.info("Source archive already exists. Skipping download.")

            if check_integrity:
                if self.sourcearchive_hashes.exists() and not self.sourcearchive_hashes.is_file():
                    raise Exception("sourcearchive_hashes is an existing non-file")
                elif force_download or check_if_exists and not self.sourcearchive_hashes.is_file():
                    self.logger.info("Downloading source archive hashes...")
                    self._download_source_hashes()
                else:
                    self.logger.info("Source hashes file already exists. Skipping download.")
        else:
            if check_integrity and hashes_path is None:
                raise Exception("Hashes path must be set with archive_path")
            if force_download:
                raise Exception("Conflicting arguments: force_download with archive_path and hashes_path")

            self.sourcearchive = archive_path
            self.sourcearchive_hashes = hashes_path

        if check_integrity:
            self.logger.info("Checking source archive integrity...")
            self._check_source_archive()

        if extract_archive:
            self.logger.info("Extracting source archive into building sandbox...")
            if use_cleaning_list:
                list_obj = self._read_list_resource(self.CLEANING_LIST)
                self._extract_source_archive(list_obj)
                for i in list_obj:
                    self.logger.warning("File does not exist in tar file: {}".format(i))
            else:
                self._extract_source_archive(list())

    def setup_build_sandbox(self, run_domain_substitution=True):
        '''
        Sets up the build sandbox. For now, this function can do domain substitution.
        '''
        if run_domain_substitution:
            self.logger.info("Running domain substitution over build sandbox...")
            def file_list_generator():
                for x in self._read_list_resource(self.DOMAIN_SUBSTITUTION_LIST):
                    yield self.sandbox_root / pathlib.Path(*pathlib.PurePosixPath(x).parts)
            self._domain_substitute(self._get_parsed_domain_regexes(), file_list_generator())
            self._ran_domain_substitution = True

    def apply_patches(self):
        # TODO: Use Python to apply patches defined in `patch_order`
        pass

    #def setup_build_utilities(self, build_gn=True, gn_command=None, python2_command=None, ninja_command="ninja"):
    #    '''
    #    Sets up the utilities required for building. For now, this is just the "gn" tool.
    #
    #    If `build_gn` is True, then the `tools/gn/bootstrap/bootstrap.py` script is invoked in the build directory to build gn.
    #    If `python2_command` is set, it must be a string of a command to invoke Python 2 for running bootstrap.py. Otherwise, the bootstrap.py path will be the executable path.
    #
    #    If `gn_command` is set, it must be a string of a command to invoke gn.
    #
    #    `build_gn` and `gn_command` are mutually exclusive.
    #    '''
    #    if build_gn and not gn_command is None:
    #        raise Exception("Conflicting arguments: build_gn and gn_path")
    #    self.ninja_command = ninja_command
    #    if build_gn:
    #        self.gn_command = self._build_gn(python2_command)
    #    else:
    #        self.gn_command = gn_command

    def setup_build_utilities(self, ninja_command="ninja"):
        self.ninja_command = ninja_command

    #def generate_build_configuration(self, gn_args=pathlib.Path("gn_args.ini"), build_output=pathlib.Path("out", "Default")):
    #    (self.sandbox_root / build_output).mkdir(parents=True, exist_ok=True)
    #    config = configparser.ConfigParser()
    #    config.read(str(gn_args))
    #    self._gn_write_args(config, build_output)
    #    self._gn_generate_ninja(build_output)

    def generate_build_configuration(self, build_output=pathlib.Path("out", "Release"), python2_command=None):
        self.logger.info("Running gyp command...")
        self._gyp_generate_ninja(self._read_list_resource(self.GYP_FLAGS), build_output, python2_command)
        self.build_output = build_output

    def build(self, build_targets=["chrome"]):
        self.logger.info("Running build command...")
        if self.build_output is None:
            raise Exception("build_output member variable is not defined. Run generate_build_configuration() first or set it manually")
        self._run_ninja(self.build_output, build_targets)

    def generate_package(self):
        # TODO: Create .tar.xz of binaries?
        pass