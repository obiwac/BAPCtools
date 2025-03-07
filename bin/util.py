# read problem settings from config files

import platform
import shutil
import time
import copy
import subprocess
import sys
import os
import re
import threading
import signal
import hashlib

from pathlib import Path
from colorama import Fore, Style

import config


def is_windows():
    return sys.platform in ['win32', 'cygwin']


# https://www.scivision.dev/python-detect-wsl/
def is_wsl():
    return 'Microsoft' in platform.uname().release


def is_mac():
    return sys.platform in ['darwin']


if not is_windows():
    import resource


def debug(*msg):
    print(Fore.CYAN, end='', file=sys.stderr)
    print('DEBUG:', *msg, end='', file=sys.stderr)
    print(Style.RESET_ALL, file=sys.stderr)


def log(msg):
    print(Fore.GREEN + 'LOG: ' + str(msg) + Style.RESET_ALL, file=sys.stderr)


def verbose(msg):
    if config.args.verbose >= 1:
        print(Fore.CYAN + 'VERBOSE: ' + str(msg) + Style.RESET_ALL, file=sys.stderr)


def warn(msg):
    print(Fore.YELLOW + 'WARNING: ' + str(msg) + Style.RESET_ALL, file=sys.stderr)
    config.n_warn += 1


def error(msg):
    if config.RUNNING_TEST:
        fatal(msg)
    print(Fore.RED + 'ERROR: ' + str(msg) + Style.RESET_ALL, file=sys.stderr)
    config.n_error += 1


def fatal(msg):
    print(Fore.RED + 'FATAL ERROR: ' + str(msg) + Style.RESET_ALL, file=sys.stderr)
    exit(1)


# A class that draws a progressbar.
# Construct with a constant prefix, the max length of the items to process, and
# the number of items to process.
# When count is None, the bar itself isn't shown.
# Start each loop with bar.start(current_item), end it with bar.done(message).
# Optionally, multiple errors can be logged using bar.log(error). If so, the
# final message on bar.done() will be ignored.
class ProgressBar:
    # Lock on all IO via this class.
    lock = threading.Lock()

    current_bar = None

    columns = shutil.get_terminal_size().columns

    if not is_windows():

        def update_columns(_, __):
            cols, rows = shutil.get_terminal_size()
            ProgressBar.columns = cols

        signal.signal(signal.SIGWINCH, update_columns)

    @staticmethod
    def item_len(item):
        if isinstance(item, str):
            return len(item)
        if isinstance(item, Path):
            return len(str(item))
        return len(item.name)

    # When needs_leading_newline is True, this will print an additional empty line before the first log message.
    def __init__(
        self, prefix, max_len=None, count=None, *, items=None, needs_leading_newline=False
    ):
        assert ProgressBar.current_bar is None
        ProgressBar.current_bar = self

        assert not (items and (max_len or count))
        assert items is not None or max_len
        if items is not None:
            count = len(items)
            if count == 0:
                max_len = 0
            else:
                max_len = max(ProgressBar.item_len(x) for x in items)
        self.prefix = prefix  # The prefix to always print
        self.item_width = max_len + 1  # The max length of the items we're processing
        self.count = count  # The number of items we're processing
        self.i = 0
        emptyline = ' ' * self.total_width() + '\r'
        self.carriage_return = emptyline if is_windows() else '\033[K'
        self.logged = False
        self.global_logged = False

        # For parallel contexts, start() will return a copy to preserve the item name.
        # The parent still holds some global state:
        # - global_logged
        # - IO lock
        # - the counter
        # - items in progress
        self.parent = None
        self.in_progress = set()
        self.item = None

        self.needs_leading_newline = needs_leading_newline

    def total_width(self):
        cols = ProgressBar.columns
        if is_windows():
            cols -= 1
        return cols

    def bar_width(self):
        if self.item_width is None:
            return None
        return self.total_width() - len(self.prefix) - 2 - self.item_width

    def update(self, count, max_len):
        self.count += count
        self.item_width = max(self.item_width, max_len + 1) if self.item_width else max_len + 1

    def add_item(self, item):
        self.count += 1
        self.item_width = max(self.item_width, ProgressBar.item_len(item))

    def clearline(self):
        if config.args.no_bar:
            return
        assert self.lock.locked()
        print(self.carriage_return, end='', flush=True, file=sys.stderr)

    def action(prefix, item, width=None, total_width=None):
        if width is not None and total_width is not None and len(prefix) + 2 + width > total_width:
            width = total_width - len(prefix) - 2
        item = '' if item is None else (item if isinstance(item, str) else item.name)
        if width is not None and len(item) > width:
            item = item[:width]
        if width is None or width <= 0:
            width = 0
        return f'{Fore.CYAN}{prefix}{Style.RESET_ALL}: {item:<{width}}'

    def get_prefix(self):
        return ProgressBar.action(self.prefix, self.item, self.item_width, self.total_width())

    def get_bar(self):
        bar_width = self.bar_width()
        if self.count is None or bar_width < 4:
            return ''
        done = (self.i - 1) * (bar_width - 2) // self.count
        text = f' {self.i}/{self.count}'
        fill = '#' * done + '-' * (bar_width - 2 - done)
        if len(text) <= len(fill):
            fill = fill[: -len(text)] + text
        return '[' + fill + ']'

    # Remove the current item from in_progress.
    def _release_item(self):
        if self.parent:
            self.parent.in_progress.remove(self.item)
            if self.parent.item is self.item:
                self.parent.item = None
        else:
            self.in_progress.remove(self.item)
        self.item = None

    # Resume the ongoing progress bar after a log/done.
    # Should only be called for the root.
    def _resume(self):
        assert self.lock.locked()
        assert self.parent is None

        if config.args.no_bar:
            return

        if len(self.in_progress) > 0:
            p = None
            if not self.item in self.in_progress:
                old = self.item
                self.item = next(iter(self.in_progress))
                p = self.item
            bar = self.get_bar()
            if bar is None or bar == '':
                print(self.get_prefix(), end='\r', flush=True, file=sys.stderr)
            else:
                print(self.get_prefix(), bar, sep='', end='\r', flush=True, file=sys.stderr)

    def start(self, item=''):
        self.lock.acquire()
        # start may only be called on the root bar.
        assert self.parent is None
        self.i += 1
        assert self.count is None or self.i <= self.count

        # assert self.item is None
        self.item = item
        self.logged = False
        self.in_progress.add(item)
        bar_copy = copy.copy(self)
        bar_copy.parent = self

        if config.args.no_bar:
            self.lock.release()
            return bar_copy

        bar = self.get_bar()
        if bar is None or bar == '':
            print(self.get_prefix(), end='\r', flush=True, file=sys.stderr)
        else:
            print(self.get_prefix(), bar, sep='', end='\r', flush=True, file=sys.stderr)

        self.lock.release()
        return bar_copy

    @staticmethod
    def _format_data(data):
        if not data:
            return ''
        prefix = '  ' if data.count('\n') <= 1 else '\n'
        return prefix + Fore.YELLOW + strip_newline(crop_output(data)) + Style.RESET_ALL

    # Log can be called multiple times to make multiple persistent lines.
    # Make sure that the message does not end in a newline.
    def log(self, message='', data='', color=Fore.GREEN, *, needs_lock=True, resume=True):
        if needs_lock:
            self.lock.acquire()

        if message is None:
            message = ''
        self.clearline()
        self.logged = True

        if self.parent:
            self.parent.global_logged = True
            if self.parent.needs_leading_newline:
                print(file=sys.stderr)
                self.parent.needs_leading_newline = False
        else:
            self.global_logged = True
            if self.needs_leading_newline:
                print(file=sys.stderr)
                self.needs_leading_newline = False

        print(
            self.get_prefix(),
            color,
            message,
            ProgressBar._format_data(data),
            Style.RESET_ALL,
            sep='',
            flush=True,
            file=sys.stderr,
        )

        if resume:
            if self.parent:
                self.parent._resume()
            else:
                self._resume()

        if needs_lock:
            self.lock.release()

    # Same as log, but only in verbose mode.
    def debug(self, message, data=''):
        if config.args.verbose:
            self.log(message, data)

    def warn(self, message='', data='', needs_lock=True):
        config.n_warn += 1
        self.log(message, data, Fore.YELLOW, needs_lock=needs_lock)

    # Error removes the current item from the in_progress set.
    def error(self, message='', data='', needs_lock=True):
        if needs_lock:
            self.lock.acquire()
        config.n_error += 1
        self.log(message, data, Fore.RED, needs_lock=False, resume=False)
        self._release_item()
        if needs_lock:
            self.lock.release()

    # Log a final line if it's an error or if nothing was printed yet and we're in verbose mode.
    def done(self, success=True, message='', data=''):
        self.lock.acquire()
        self.clearline()

        if self.item is None:
            self.lock.release()
            return

        if not self.logged:
            if not success:
                config.n_error += 1
            if config.args.verbose or not success:
                self.log(message, data, needs_lock=False, color=Fore.GREEN if success else Fore.RED)

        self._release_item()
        if self.parent:
            self.parent._resume()

        self.lock.release()
        return

    # Log an intermediate line if it's an error or we're in verbose mode.
    # Return True when something was printed
    def part_done(self, success=True, message='', data='', warn_instead_of_error=False):
        if not success:
            config.n_error += 1
        if config.args.verbose or not success:
            self.lock.acquire()
            if success:
                self.log(message, data, needs_lock=False)
            else:
                if warn_instead_of_error:
                    self.warn(message, data, needs_lock=False)
                else:
                    self.error(message, data, needs_lock=False)
            if self.parent:
                self.parent._resume()
            self.lock.release()
            return True
        return False

    # Print a final 'Done' message in case nothing was printed yet.
    # When 'message' is set, always print it.
    def finalize(self, *, print_done=True, message=None):
        self.lock.acquire()
        self.clearline()
        assert self.parent is None
        assert self.count is None or self.i == self.count
        assert self.item is None
        # At most one of print_done and message may be passed.
        if message:
            assert print_done is True

        # If nothing was logged, we don't need the super wide spacing before the final 'DONE'.
        if not self.global_logged and not message:
            self.item_width = 0

        # Print 'DONE' when nothing was printed yet but a summary was requested.
        if print_done and not self.global_logged and not message:
            message = f'{Fore.GREEN}Done{Style.RESET_ALL}'

        if message:
            print(self.get_prefix(), message, sep='', file=sys.stderr)

        # When something was printed, add a newline between parts.
        if self.global_logged:
            print(file=sys.stderr)

        self.lock.release()

        assert ProgressBar.current_bar is not None
        ProgressBar.current_bar = None

        return self.global_logged


# Given a command line argument, return the first match:
# - absolute
# - relative to the 'type' directory for the current problem
# - relative to the problem directory
# - relative to the contest directory
# - relative to the current working directory
#
# Pass suffixes = ['.in'] to also try to find the file with the given suffix appended.
def get_basedirs(problem, type):
    p = problem.path
    return [p / type, p, p.parent, config.current_working_directory]


# Python 3.9
# True when child is a Path inside parent Path.
# Both must be absolute.
def is_relative_to(parent, child):
    return child == parent or parent in child.parents


def resolve_path_argument(problem, path, type, suffixes=[]):
    if path.is_absolute():
        return path
    for suffix in suffixes + [None]:
        suffixed_path = path if suffix is None else path.with_suffix(suffix)
        for basedir in get_basedirs(problem, type):
            p = basedir / suffixed_path
            if p.exists():
                return p
    warn(f'{path} not found')
    return None


# Drops the first two path components <problem>/<type>/
def print_name(path, keep_type=False):
    return str(Path(*path.parts[1 if keep_type else 2 :]))


try:
    import ruamel.yaml

    has_ryaml = True
    ryaml = ruamel.yaml.YAML(typ='rt')
    ryaml.default_flow_style = False
    ryaml.indent(mapping=2, sequence=4, offset=2)
except:
    has_ryaml = False


def parse_yaml(data, path=None):
    # First try parsing with ruamel.yaml.
    # If not found, use the normal yaml lib instead.
    if has_ryaml:
        try:
            return ryaml.load(data)
        except ruamel.yaml.constructor.DuplicateKeyError as error:
            if path is not None:
                fatal(f'Duplicate key in yaml file {path}!\n{error.args[0]}\n{error.args[2]}')
            else:
                fatal(f'Duplicate key in yaml object!\n{str(error)}')

    else:
        try:
            import yaml

            return yaml.safe_load(data)
        except:
            fatal(f'Failed to parse {path}.')


def read_yaml(path):
    assert path.is_file()
    return parse_yaml(path.read_text(), path=path)


# Wrapper around read_yaml that returns an empty dictionary by default.
def read_yaml_settings(path):
    settings = {}
    if path.is_file():
        config = read_yaml(path)
        if config is None:
            return None
        if isinstance(config, list):
            return config
        for key, value in config.items():
            settings[key] = '' if value is None else value
    return settings


# Only allow one thread to write at the same time. Else, e.g., generating test cases in parallel goes wrong.
write_yaml_lock = threading.Lock()


# Writing a yaml file only works when ruamel.yaml is loaded. Check if `has_ryaml` is True before using.
def write_yaml(data, path):
    if not has_ryaml:
        error(
            'This operation requires the ruamel.yaml python3 library. Install python[3]-ruamel.yaml.'
        )
        exit(1)
    with write_yaml_lock:
        ryaml.dump(
            data,
            path,
            # Remove spaces at the start of each (non-commented) line, caused by the indent configuration.
            # This is only needed when the YAML data is a list of items, like in the problems.yaml file.
            # See also: https://stackoverflow.com/a/58773229
            transform=(
                (
                    lambda yaml_str: "\n".join(
                        line if line.strip().startswith('#') else line[2:]
                        for line in yaml_str.split("\n")
                    )
                )
                if isinstance(data, list)
                else None
            ),
        )


# glob, but without hidden files
def glob(path, expression, include_hidden=False):
    def keep(p):
        if not include_hidden:
            for d in p.parts:
                if d[0] == '.':
                    return False

        if p.suffix in ['.template', '.disabled']:
            return False

        if config.RUNNING_TEST:
            suffixes = p.suffixes
            if len(suffixes) >= 1 and suffixes[-1] == '.bad':
                return False
            if len(suffixes) >= 2 and suffixes[-2] == '.bad':
                return False

        return True

    return sorted(p for p in path.glob(expression) if keep(p))


def strip_newline(s):
    if s.endswith('\n'):
        return s[:-1]
    else:
        return s


# When output is True, copy the file when args.cp is true.
def ensure_symlink(link, target, output=False, relative=False):
    # On windows, always copy.
    if is_windows():
        if link.exists() or link.is_symlink():
            link.unlink()
        shutil.copyfile(target, link)
        return

    # For output files: copy them on Windows, or when --cp is passed.
    if output and (is_windows() or config.args.cp):
        if link.exists() or link.is_symlink():
            link.unlink()
        shutil.copyfile(target, link)
        return

    # Do nothing if link already points to the right target.
    if link.is_symlink() and link.resolve() == target.resolve():
        is_absolute = os.readlink(link)
        if not relative and is_absolute:
            return
        # if relative and not is_absolute: return

    if link.is_symlink() or link.exists():
        if link.is_dir():
            shutil.rmtree(link)
        else:
            link.unlink()
    if relative:
        # Rewrite target to be relative to link.
        rel_target = os.path.relpath(target, link.parent)
        os.symlink(rel_target, link)
    else:
        link.symlink_to(target.resolve())


def substitute(data, variables):
    if variables is None:
        return data
    for key in variables:
        r = ''
        if variables[key] != None:
            r = variables[key]
        data = data.replace('{%' + key + '%}', str(r))
    return data


def copy_and_substitute(inpath, outpath, variables):
    try:
        data = inpath.read_text()
    except UnicodeDecodeError:
        # skip this file
        log(f'File "{inpath}" is not a text file.')
        return
    data = substitute(data, variables)
    if outpath.is_symlink():
        outpath.unlink()
    outpath.write_text(data)


def substitute_file_variables(path, variables):
    copy_and_substitute(path, path, variables)


def substitute_dir_variables(dirname, variables):
    for path in dirname.rglob('*'):
        if path.is_file():
            substitute_file_variables(path, variables)


# copies a directory recursively and substitutes {%key%} by their value in text files
# reference: https://docs.python.org/3/library/shutil.html#copytree-example
def copytree_and_substitute(
    src, dst, variables, exist_ok=True, *, preserve_symlinks=True, base=None
):
    if base is None:
        base = src

    if preserve_symlinks and os.path.islink(src):
        shutil.copy(src, dst, follow_symlinks=False)
    elif os.path.islink(src) and src.resolve().is_relative_to(base):
        shutil.copy(src, dst, follow_symlinks=False)
    elif os.path.isdir(src):
        names = os.listdir(src)
        os.makedirs(dst, exist_ok=exist_ok)
        errors = []
        for name in names:
            try:
                srcFile = src / name
                dstFile = dst / name

                copytree_and_substitute(
                    srcFile,
                    dstFile,
                    variables,
                    exist_ok,
                    preserve_symlinks=preserve_symlinks,
                    base=base,
                )
            except OSError as why:
                errors.append((srcFile, dstFile, str(why)))
            # catch the Error from the recursive copytree so that we can
            # continue with other files
            except Exception as err:
                errors.append(err.args[0])
        if errors:
            raise Exception(errors)

    elif dst.exists():
        warn(f'File "{dst}" already exists, skipping...')
    else:
        try:
            data = src.read_text()
            data = substitute(data, variables)
            dst.write_text(data)
        except UnicodeDecodeError:
            # Do not substitute for binary files.
            dst.write_bytes(src.read_bytes())


def crop_output(output):
    if config.args.error:
        return output

    lines = output.split('\n')
    numlines = len(lines)
    cropped = False
    # Cap number of lines
    if numlines > 30:
        output = '\n'.join(lines[:25])
        output += '\n'
        cropped = True

    # Cap total length.
    if len(output) > 2000:
        output = output[:2000]
        output += ' ...\n'
        cropped = True

    if cropped:
        output += Fore.YELLOW + 'Use -e to show more.' + Style.RESET_ALL
    return output


# TODO: Move this to Problem.settings and read limits.memory variable from problem.yaml.
# Return memory limit in MB.
def get_memory_limit(kwargs=None):
    memory_limit = 2048  # 2GB
    if config.args.memory:
        if config.args.memory != 'unlimited':
            memory_limit = int(config.args.memory)
        else:
            memory_limit = None  # disabled
    if kwargs and 'memory' in kwargs:
        memory_limit = kwargs['memory']
        kwargs.pop('memory')
    return memory_limit


class ExecResult:
    def __init__(self, ok, duration, err, out, verdict=None, print_verdict=None):
        self.ok = ok
        self.duration = duration
        self.err = err
        self.out = out
        self.verdict = verdict
        self.print_verdict_ = print_verdict

    def print_verdict(self):
        if self.print_verdict_:
            return self.print_verdict_
        return self.verdict


def limit_setter(command, timeout, memory_limit):
    def setlimits():
        if timeout:
            resource.setrlimit(resource.RLIMIT_CPU, (timeout + 1, timeout + 1))

        # Increase the max stack size from default to the max available.
        if not is_mac():
            resource.setrlimit(
                resource.RLIMIT_STACK, (resource.RLIM_INFINITY, resource.RLIM_INFINITY)
            )

        if (
            memory_limit
            and not Path(command[0]).name in ['java', 'javac', 'kotlin', 'kotlinc']
            and not is_mac()
        ):
            resource.setrlimit(
                resource.RLIMIT_AS, (memory_limit * 1024 * 1024, memory_limit * 1024 * 1024)
            )

        # Disable coredumps.
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    return setlimits


# Subclass Popen to get rusage information.
class ResourcePopen(subprocess.Popen):
    # If wait4 is available, store resource usage information.
    if 'wait4' in dir(os):

        def _try_wait(self, wait_flags):
            """All callers to this function MUST hold self._waitpid_lock."""
            try:
                (pid, sts, res) = os.wait4(self.pid, wait_flags)
            except ChildProcessError:
                # This happens if SIGCLD is set to be ignored or waiting
                # for child processes has otherwise been disabled for our
                # process.  This child is dead, we can't get the status.
                pid = self.pid
                sts = 0
            else:
                self.rusage = res
            return (pid, sts)

    else:

        def _try_wait(self, wait_flags):
            """All callers to this function MUST hold self._waitpid_lock."""
            try:
                (pid, sts) = os.waitpid(self.pid, wait_flags)
            except ChildProcessError:
                # This happens if SIGCLD is set to be ignored or waiting
                # for child processes has otherwise been disabled for our
                # process.  This child is dead, we can't get the status.
                pid = self.pid
                sts = 0
            else:
                self.rusage = None
            return (pid, sts)


# Run `command`, returning stderr if the return code is unexpected.
def exec_command(command, expect=0, crop=True, **kwargs):
    # By default: discard stdout, return stderr
    if 'stdout' not in kwargs or kwargs['stdout'] is True:
        kwargs['stdout'] = subprocess.PIPE
    if 'stderr' not in kwargs or kwargs['stderr'] is True:
        kwargs['stderr'] = subprocess.PIPE

    # Convert any Pathlib objects to string.
    command = [str(x) for x in command]

    if config.args.verbose >= 2:
        if 'cwd' in kwargs:
            print('cd', kwargs['cwd'], '; ', end='', file=sys.stderr)
        else:
            print('cd', Path.cwd(), '; ', end='', file=sys.stderr)
        print(*command, end='', file=sys.stderr)
        if 'stdin' in kwargs:
            print(' < ', kwargs['stdin'].name, end='', file=sys.stderr)
        print(file=sys.stderr)

    timeout = config.DEFAULT_TIMEOUT
    if 'timeout' in kwargs:
        if kwargs['timeout'] is None:
            timeout = None
        elif kwargs['timeout']:
            timeout = kwargs['timeout']
        kwargs.pop('timeout')

    if (is_windows() or is_wsl()) and 'memory' in kwargs:
        kwargs.pop('memory')

    process = None

    def interrupt_handler(sig, frame):
        nonlocal process
        process.kill()
        # Extra newline to not overwrite progress bars
        print(file=sys.stderr)
        fatal('Running interrupted')

    if threading.current_thread() is threading.main_thread():
        old_handler = signal.signal(signal.SIGINT, interrupt_handler)

    did_timeout = False

    tstart = time.monotonic()
    try:
        if not is_windows() and not is_wsl():
            process = ResourcePopen(
                command,
                preexec_fn=limit_setter(command, timeout, get_memory_limit(kwargs)),
                **kwargs,
            )
        else:
            process = ResourcePopen(command, **kwargs)
        (stdout, stderr) = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Timeout expired.
        did_timeout = True
        process.kill()
        (stdout, stderr) = process.communicate()
    except PermissionError as e:
        # File is likely not executable.
        stdout = None
        stderr = str(e)
        return ExecResult(-1, 0, stderr, stdout)
    except OSError as e:
        # File probably doesn't exist.
        stdout = None
        stderr = str(e)
        return ExecResult(-1, 0, stderr, stdout)
    tend = time.monotonic()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, old_handler)

    # -2 corresponds to SIGINT, i.e. keyboard interrupt / CTRL-C.
    if process.returncode == -2:
        if threading.current_thread() is threading.main_thread():
            fatal('Child process interrupted.')
        else:
            raise ChildProcessError()

    def maybe_crop(s):
        return crop_output(s) if crop else s

    ok = True if process.returncode == expect else process.returncode
    err = maybe_crop(stderr.decode('utf-8', 'replace')) if stderr is not None else None
    out = maybe_crop(stdout.decode('utf-8', 'replace')) if stdout is not None else None

    if hasattr(process, 'rusage'):
        duration = process.rusage.ru_utime + process.rusage.ru_stime
        # It may happen that the Rusage is low, even though a timeout was raised, i.e. when calling sleep().
        # To prevent under-reporting the duration, we take the max with wall time in this case.
        if did_timeout:
            duration = max(tend - tstart, duration)
    else:
        duration = tend - tstart

    return ExecResult(ok, duration, err, out)


def inc_label(label):
    for x in range(len(label) - 1, -1, -1):
        if label[x] != 'Z':
            label = label[:x] + chr(ord(label[x]) + 1) + label[x + 1 :]
            return label
        label = label[:x] + 'A' + label[x + 1 :]
    return 'A' + label

def hash_file(file, buffer_size=65536):
    assert file.is_file()
    sha = hashlib.sha256(usedforsecurity=False)

    with open(file, 'rb') as f:
        while True:
            data = f.read(buffer_size)
            if not data:
                break
            sha.update(data)

    return sha.hexdigest();

def combine_hashes(list):
    list.sort()
    hasher = hashlib.sha256(usedforsecurity=False)
    for item in list:
        hasher.update(item.encode())
    return hasher.hexdigest()
