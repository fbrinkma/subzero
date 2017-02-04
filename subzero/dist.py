# This file was originally taken from cx_freeze by Anthony Tuininga, and is licensed under the  PSF license.

import distutils.command.build
import distutils.version
import inspect
import json
import ntpath
import os
import pkgutil
import shutil
import subprocess
import sys
import uuid
from subprocess import CalledProcessError

import PyInstaller.__main__
from PyInstaller.building.makespec import main as makespec_main
from PyInstaller.utils.hooks import collect_submodules, get_module_file_attribute
from packaging import version
from pkg_resources import EntryPoint, Requirement

# from pyspin.spin import make_spin, Spin1

if sys.version_info >= (3, 4):
    from contextlib import suppress
else:
    from contextlib2 import suppress

__all__ = ["build_exe", "setup"]


class build_exe(distutils.core.Command):
    description = "build executables from Python scripts"
    user_options = []
    boolean_options = []
    _excluded_args = [
        'scripts',
        'specpath',
    ]


    @classmethod
    def makespec_args(cls):
        names = ['datas']  # signature does not detect datas for some reason
        for name, parameter in inspect.signature(makespec_main).parameters.items():
            if name not in (cls._excluded_args + ['args', 'kwargs']):
                names.append(name)

        return names

    @staticmethod
    def decode(bytes_or_string):
        if isinstance(bytes_or_string, bytes):
            return bytes_or_string.decode()
        else:
            return bytes_or_string

    @staticmethod
    def is_binary(file):
        return file.endswith((
            '.so',
            '.pyd',
            '.dll',
        ))

    @staticmethod
    def rename_script(executable):
        # Per issue #32.
        new_script_name = '{}.{}.py'.format(executable.script, str(uuid.uuid4()))
        os.rename(executable.script, new_script_name)
        executable.script = new_script_name


    @staticmethod
    def build_dir():
        return "exe.{}-{}".format(distutils.util.get_platform(), sys.version[0:3])

    def add_to_path(self, name):
        sourceDir = getattr(self, name.lower())
        if sourceDir is not None:
            sys.path.insert(0, sourceDir)

    def initialize_options(self):
        distutils.command.build.build.initialize_options(self)
        self.build_exe = None
        self.optimize_imports = True
        self.executables = []
        self._script_names = []

        for name in self.makespec_args():
            if not getattr(self, name, None):
                setattr(self, name, None)

    def finalize_options(self):
        distutils.command.build.build.finalize_options(self)
        if self.build_exe is None:
            self.build_exe = os.path.join(self.build_base, self.build_dir())

        try:
            self.distribution.install_requires = list(self.distribution.install_requires)
        except TypeError:
            self.distribution.install_requires = []

        try:
            self.distribution.packages = list(self.distribution.packages)
        except TypeError:
            self.distribution.packages = []

        try:
            self.distribution.scripts = list(self.distribution.scripts)
        except TypeError:
            self.distribution.scripts = []

        self.distribution.entry_points.setdefault('console_scripts', [])

        if not hasattr(self.distribution, 'setup_requires'):
            self.distribution.setup_requires = []

    def run(self):
        try:
            entry_points = EntryPoint.parse_map(self.distribution.entry_points)['console_scripts']
        except KeyError:
            entry_points = []
        try:
            options = {}
            for key, value in dict(self.distribution.command_options['build_exe']).items():
                options[key] = value[1]
        except (KeyError, TypeError):
            options = {}

        scripts = self.distribution.scripts
        for required_directory in [self.build_temp, self.build_exe]:
            shutil.rmtree(required_directory, ignore_errors=True)
            os.makedirs(required_directory, exist_ok=True)

        for entry_point in entry_points.values():
            scripts.append(self._generate_script(entry_point, self.build_temp))

        lib_dirs = ['lib', 'lib{}'.format(self.build_dir()[3:])]
        for lib_dir in lib_dirs:
            shutil.rmtree(os.path.join(self.build_base, lib_dir), ignore_errors=True)

        self.run_command('build')

        for default_option in ['pathex', 'hiddenimports', 'binaries']:
            options.setdefault(default_option, [])

        # by convention, all paths appended to py_options must be absolute
        options['hiddenimports'].extend(self.distribution.install_requires)
        for lib_dir in lib_dirs:
            if os.path.isdir(os.path.join(self.build_base, lib_dir)):
                options['pathex'].append(os.path.abspath(os.path.join(self.build_base, lib_dir)))

        if not options['pathex']:
            raise ValueError('Unable to find lib directory!')

        if version.parse(sys.version[0:3]) >= version.parse('3.4'):
            for package in self.distribution.packages:
                options['hiddenimports'].extend(collect_submodules(package))

        options['specpath'] = os.path.abspath(self.build_temp)
        options['pathex'].append(os.path.abspath(self.build_temp))

        if not self.optimize_imports:
            self.discover_dependencies(options)

        executables = []
        for script, executable in zip(scripts, self.executables):
            executable = executable or Executable(script)
            executable.script = script
            executable._options = dict(options, **executable.options)
            executable._options['name'] = '.'.join(ntpath.basename(script).split('.')[:-1])

            executables.append(executable)

        for executable in executables:
            self.rename_script(executable)

        names = [executable.options['name'] for executable in executables]
        for executable in executables:
            self._freeze(executable, self.build_temp, self.build_exe)

        for name in names[1:]:
            self.move_tree(os.path.join(self.build_exe, name), os.path.join(self.build_exe, names[0]))

        self.move_tree(os.path.join(self.build_exe, names[0]), self.build_exe)

        shutil.rmtree(self.build_temp, ignore_errors=True)

        # TODO: Compare file hashes to make sure we haven't replaced files with a different version
        for name in names:
            shutil.rmtree(os.path.join(self.build_exe, name), ignore_errors=True)

    # @make_spin(Spin1, 'Compiling module file locations...')
    def _compile_modules(self):
        modules = {}

        for module_finder, name, ispkg in pkgutil.walk_packages():
            for attempt in range(2):
                with suppress((AttributeError, ImportError)):
                    if attempt == 0:
                        loader = module_finder.find_spec(name).loader
                        filename = loader.get_filename(name)
                    elif attempt == 1:
                        filename = get_module_file_attribute(name)
                    break
            else:
                continue

            modules[os.path.abspath(filename)] = name

        return modules

    # @make_spin(Spin1, 'Compiling project requirements...')
    def _compile_requirements(self):
        packages = []
        for requirement in self.distribution.setup_requires:
            requirement = Requirement.parse(requirement)
            packages.append(requirement.key)

        entries = json.loads(self.decode(subprocess.check_output(['pipdeptree', '--json'])))
        updated = True
        while updated:
            updated = False
            for entry in entries:
                if entry['package']['key'] in packages:
                    for dependency in entry['dependencies']:
                        if dependency['key'] not in packages:
                            packages.append(dependency['key'])
                            updated = True

        location_string = 'Location:'
        files_string = 'Files:'
        module_files = set()
        binary_files = set()

        for package in packages:
            in_header = True
            root = None
            with suppress(CalledProcessError):
                for line in self.decode(subprocess.check_output(['pip', 'show', '-f', package])).splitlines():
                    line = line.strip()
                    if in_header and line.startswith(location_string):
                        root = line[len(location_string):]
                    if in_header and line.startswith(files_string):
                        assert root is not None
                        in_header = False
                        continue
                    elif not in_header:
                        full_path = os.path.abspath(os.path.join(root, line.strip()))
                        if line.endswith('.py') or line.endswith('.pyc'):
                            module_files.add(full_path)
                        if self.is_binary(line):
                            binary_files.add(full_path)

        return module_files, binary_files

    def discover_dependencies(self, options):
        module_files = self._compile_modules()
        required_module_files, required_binary_files = self._compile_requirements()

        for required_file in required_module_files:
            try:
                options['hiddenimports'].append(module_files[required_file])
            except KeyError:
                print('Unable to collect module for {}'.format(required_file))

        for required_file in required_binary_files:
            # FIXME: Add to binaries rather than simply appending to pathex.
            options['pathex'].append(os.path.dirname(required_file))

        options['pathex'] = list(set(options['pathex']))

    @staticmethod
    def move_tree(sourceRoot, destRoot):
        if not os.path.exists(destRoot):
            return False
        ok = True
        for path, dirs, files in os.walk(sourceRoot):
            relPath = os.path.relpath(path, sourceRoot)
            destPath = os.path.join(destRoot, relPath)
            if not os.path.exists(destPath):
                os.makedirs(destPath)
            for file in files:
                destFile = os.path.join(destPath, file)
                if os.path.isfile(destFile):
                    print("Skipping existing file: {}".format(os.path.join(relPath, file)))
                    ok = False
                    continue
                srcFile = os.path.join(path, file)
                # print "rename", srcFile, destFile
                os.rename(srcFile, destFile)
        for path, dirs, files in os.walk(sourceRoot, False):
            if len(files) == 0 and len(dirs) == 0:
                os.rmdir(path)
        return ok

    def _generate_script(self, entry_point, workpath):
        """
        Generates a script given an entry point.
        :param entry_point:
        :param workpath:
        :return: The script location
        """

        # note that build_scripts appears to work sporadically

        # entry_point.attrs is tuple containing function
        # entry_point.module_name is string representing module name
        # entry_point.name is string representing script name

        # script name must not be a valid module name to avoid name clashes on import
        script_path = os.path.join(workpath, '{}.py'.format(entry_point.name))
        with open(script_path, 'w+') as fh:
            fh.write("import {0}\n".format(entry_point.module_name))
            fh.write("{0}.{1}()\n".format(entry_point.module_name, '.'.join(entry_point.attrs)))
            for package in self.distribution.packages + self.distribution.install_requires:
                fh.write("import {0}\n".format(package))

        return script_path

    @staticmethod
    def _freeze(executable, workpath, distpath):

        with suppress(OSError):
            os.remove(os.path.join(executable.options['specpath'], '{}.spec'.format(executable.options['name'])))

        spec_file = PyInstaller.__main__.run_makespec([executable.script], **executable.options)
        PyInstaller.__main__.run_build(None, spec_file, noconfirm=True, workpath=workpath, distpath=distpath)
        # os.remove(spec_file)


class Executable(object):
    def __str__(self):
        return self.script

    def __init__(self, script, **kwargs):
        self.script = script
        self._options = {}

        for name in kwargs:
            if name in build_exe.makespec_args():
                self._options[name] = kwargs[name]

    @property
    def options(self):
        return self._options
