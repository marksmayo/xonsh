# -*- coding: utf-8 -*-
"""Implements the xonsh history object."""
import argparse
import os
from functools import lru_cache, partial
from os import listdir
from operator import itemgetter
import uuid
import time
from datetime import datetime
import builtins
from glob import iglob
from collections import deque, Sequence, OrderedDict
from threading import Thread, Condition
from json.decoder import JSONDecodeError

from xonsh import lazyjson
from xonsh.tools import ensure_int_or_slice, to_history_tuple
from xonsh import diff_history


def _gc_commands_to_rmfiles(hsize, files):
    """Return the history files to remove to get under the command limit."""
    rmfiles = []
    n = 0
    ncmds = 0
    for ts, fcmds, f in files[::-1]:
        if fcmds == 0:
            # we need to make sure that 'empty' history files don't hang around
            rmfiles.append((ts, fcmds, f))
        if ncmds + fcmds > hsize:
            break
        ncmds += fcmds
        n += 1
    rmfiles += files[:-n]
    return rmfiles


def _gc_files_to_rmfiles(hsize, files):
    """Return the history files to remove to get under the file limit."""
    rmfiles = files[:-hsize] if len(files) > hsize else []
    return rmfiles


def _gc_seconds_to_rmfiles(hsize, files):
    """Return the history files to remove to get under the age limit."""
    rmfiles = []
    now = time.time()
    for ts, _, f in files:
        if (now - ts) < hsize:
            break
        rmfiles.append((None, None, f))
    return rmfiles


def _gc_bytes_to_rmfiles(hsize, files):
    """Return the history files to remove to get under the byte limit."""
    rmfiles = []
    n = 0
    nbytes = 0
    for _, _, f in files[::-1]:
        fsize = os.stat(f).st_size
        if nbytes + fsize > hsize:
            break
        nbytes += fsize
        n += 1
    rmfiles = files[:-n]
    return rmfiles


class HistoryGC(Thread):
    """Shell history garbage collection."""

    def __init__(self, wait_for_shell=True, size=None, *args, **kwargs):
        """Thread responsible for garbage collecting old history.

        May wait for shell (and for xonshrc to have been loaded) to start work.
        """
        super().__init__(*args, **kwargs)
        self.daemon = True
        self.size = size
        self.wait_for_shell = wait_for_shell
        self.start()
        self.gc_units_to_rmfiles = {'commands': _gc_commands_to_rmfiles,
                                    'files': _gc_files_to_rmfiles,
                                    's': _gc_seconds_to_rmfiles,
                                    'b': _gc_bytes_to_rmfiles}

    def run(self):
        while self.wait_for_shell:
            time.sleep(0.01)
        env = builtins.__xonsh_env__  # pylint: disable=no-member
        if self.size is None:
            hsize, units = env.get('XONSH_HISTORY_SIZE')
        else:
            hsize, units = to_history_tuple(self.size)
        files = self.files(only_unlocked=True)
        rmfiles_fn = self.gc_units_to_rmfiles.get(units)
        if rmfiles_fn is None:
            raise ValueError('Units type {0!r} not understood'.format(units))

        for _, _, f in rmfiles_fn(hsize, files):
            try:
                os.remove(f)
            except OSError:
                pass

    def files(self, only_unlocked=False):
        """Find and return the history files. Optionally locked files may be
        excluded.

        This is sorted by the last closed time. Returns a list of (timestamp,
        file) tuples.
        """
        _ = self  # this could be a function but is intimate to this class
        # pylint: disable=no-member
        xdd = os.path.expanduser(builtins.__xonsh_env__.get('XONSH_DATA_DIR'))
        xdd = os.path.abspath(xdd)
        fs = [f for f in iglob(os.path.join(xdd, 'xonsh-*.json'))]
        files = []
        for f in fs:
            try:
                lj = lazyjson.LazyJSON(f, reopen=False)
                if only_unlocked and lj['locked']:
                    continue
                # info: closing timestamp, number of commands, filename
                files.append((lj['ts'][1] or time.time(),
                              len(lj.sizes['cmds']) - 1,
                              f))
                lj.close()
            except (IOError, OSError, ValueError):
                continue
        files.sort()
        return files


class HistoryFlusher(Thread):
    """Flush shell history to disk periodically."""

    def __init__(self, filename, buffer, queue, cond, at_exit=False, *args,
                 **kwargs):
        """Thread for flushing history."""
        super(HistoryFlusher, self).__init__(*args, **kwargs)
        self.filename = filename
        self.buffer = buffer
        self.queue = queue
        queue.append(self)
        self.cond = cond
        self.at_exit = at_exit
        if at_exit:
            self.dump()
            queue.popleft()
        else:
            self.start()

    def run(self):
        with self.cond:
            self.cond.wait_for(self.i_am_at_the_front)
            self.dump()
            self.queue.popleft()

    def i_am_at_the_front(self):
        """Tests if the flusher is at the front of the queue."""
        return self is self.queue[0]

    def dump(self):
        """Write the cached history to external storage."""
        with open(self.filename, 'r', newline='\n') as f:
            hist = lazyjson.LazyJSON(f).load()
        hist['cmds'].extend(self.buffer)
        if self.at_exit:
            hist['ts'][1] = time.time()  # apply end time
            hist['locked'] = False
        with open(self.filename, 'w', newline='\n') as f:
            lazyjson.dump(hist, f, sort_keys=True)


class CommandField(Sequence):
    """A field in the 'cmds' portion of history."""

    def __init__(self, field, hist, default=None):
        """Represents a field in the 'cmds' portion of history.

        Will query the buffer for the relevant data, if possible. Otherwise it
        will lazily acquire data from the file.

        Parameters
        ----------
        field : str
            The name of the field to query.
        hist : History object
            The history object to query.
        default : optional
            The default value to return if key is not present.
        """
        self.field = field
        self.hist = hist
        self.default = default

    def __len__(self):
        return len(self.hist)

    def __getitem__(self, key):
        size = len(self)
        if isinstance(key, slice):
            return [self[i] for i in range(*key.indices(size))]
        elif not isinstance(key, int):
            raise IndexError(
                'CommandField may only be indexed by int or slice.')
        elif size == 0:
            raise IndexError('CommandField is empty.')
        # now we know we have an int
        key = size + key if key < 0 else key  # ensure key is non-negative
        bufsize = len(self.hist.buffer)
        if size - bufsize <= key:  # key is in buffer
            return self.hist.buffer[key + bufsize - size].get(
                self.field, self.default)
        # now we know we have to go into the file
        queue = self.hist._queue
        queue.append(self)
        with self.hist._cond:
            self.hist._cond.wait_for(self.i_am_at_the_front)
            with open(self.hist.filename, 'r', newline='\n') as f:
                lj = lazyjson.LazyJSON(f, reopen=False)
                rtn = lj['cmds'][key].get(self.field, self.default)
                if isinstance(rtn, lazyjson.Node):
                    rtn = rtn.load()
            queue.popleft()
        return rtn

    def i_am_at_the_front(self):
        """Tests if the command field is at the front of the queue."""
        return self is self.hist._queue[0]


def _all_xonsh_formatter(*args):
    """
    Returns all history as found in XONSH_DATA_DIR.

    return format: (name, start_time, index)
    """
    data_dir = builtins.__xonsh_env__.get('XONSH_DATA_DIR')
    data_dir = os.path.expanduser(data_dir)

    files = [os.path.join(data_dir, f) for f in listdir(data_dir)
             if f.startswith('xonsh-') and f.endswith('.json')]
    file_hist = list()
    for f in files:
        try:
            json_file = lazyjson.LazyJSON(f, reopen=False)
            file_hist.append(json_file.load()['cmds'])
        except JSONDecodeError:
            # Invalid json file
            pass
    commands = [(c['inp'].replace('\n', ''), c['ts'][0])
                for commands in file_hist for c in commands if c]
    commands.sort(key=itemgetter(1))
    return [(c, t, ind) for ind, (c, t) in enumerate(commands)]


def _curr_session_formatter(hist):
    """
    Take in History object and return command list tuple with
    format: (name, start_time, index)
    """
    if not hist:
        hist = builtins.__xonsh_history__
    if not hist:
        return None
    start_times = [start for start, end in hist.tss]
    names = [name[:-1] if name.endswith('\n') else name
             for name in hist.inps]
    commands = enumerate(zip(names, start_times))
    return [(c, t, ind) for ind, (c, t) in commands]


def _zsh_hist_formatter(location=None):
    if not location:
        location = os.path.join('~', '.zsh_history')
    z_hist_formatted = list()
    z_path = os.path.expanduser(location)
    if os.path.isfile(z_path):
        with open(z_path, 'r') as z_file:
            z_txt = z_file.read()
            z_txt = z_txt.encode('utf-8', 'replace').decode('utf-8')
            z_hist = z_txt.splitlines()
            try:
                if z_hist:
                    for ind, line in enumerate(z_hist):
                        start_time, command = line.split(';')
                        try:
                            start_time = float(start_time.split(':')[1])
                        except ValueError:
                            start_time = -1
                        z_hist_formatted.append((command, start_time, ind))
                    return z_hist_formatted
            except Exception as e:
                print("There was a problem parsing {}.".format(z_path))
                raise e
                return None

    else:
        print("No zsh history file found at: {}".format(z_path))
        return None


def _bash_hist_formatter(location=None):
    if not location:
        location = os.path.join('~', '.bash_history')
    bash_hist_formatted = list()
    b_path = os.path.expanduser(location)
    if os.path.isfile(b_path):
        try:
            with open(b_path, 'r') as bash_file:
                b_txt = bash_file.read()
                b_txt = b_txt.encode('utf-8', 'replace').decode('utf-8')
                bash_hist = b_txt.splitlines()
                if bash_hist:
                    for ind, command in enumerate(bash_hist):
                        bash_hist_formatted.append((command, 0.0, ind))
                    return bash_hist_formatted
        except:
            print("There was a problem parsing {}.".format(b_path))
            return None
    else:
        print("No bash history file found at: {}".format(b_path))
        return None


@lru_cache()
def _create_parser():
    """Create a parser for the "history" command."""
    p = argparse.ArgumentParser(prog='history',
                                description='Tools for dealing with history')
    subp = p.add_subparsers(title='action', dest='action')
    # show action
    session = subp.add_parser('session',
                              help='displays session history, default action')
    session.add_argument('-r', dest='reverse', default=False,
                         action='store_true',
                         help='reverses the direction')
    session.add_argument('n', nargs='?', default=None,
                         help='display n\'th history entry if n is a simple '
                              'int, or range of entries if it is Python '
                              'slice notation')
    # 'id' subcommand
    show_all = subp.add_parser('all',
                               help='displays history from all sessions')
    show_all.add_argument('-r', dest='reverse', default=False,
                          action='store_true',
                          help='reverses the direction')
    show_all.add_argument('n', nargs='?', default=None,
                          help='display n\'th history entry if n is a '
                               'simple int, or range of entries if it '
                               'is Python slice notation')
    zsh = subp.add_parser('zsh', help='displays history from zsh sessions')
    zsh.add_argument('-r', dest='reverse', default=False,
                     action='store_true',
                     help='reverses the direction')
    zsh.add_argument('n', nargs='?', default=None,
                     help='display n\'th history entry if n is a '
                     'simple int, or range of entries if it '
                     'is Python slice notation')
    bash = subp.add_parser('bash', help='displays history from bash sessions')
    bash.add_argument('-r', dest='reverse', default=False,
                      action='store_true',
                      help='reverses the direction')
    bash.add_argument('n', nargs='?', default=None,
                      help='display n\'th history entry if n is a '
                      'simple int, or range of entries if it '
                      'is Python slice notation')
    subp.add_parser('id', help='displays the current session id')
    # 'file' subcommand
    subp.add_parser('file', help='displays the current history filename')
    # 'info' subcommand
    info = subp.add_parser('info', help=('displays information about the '
                                         'current history'))
    info.add_argument('--json', dest='json', default=False,
                      action='store_true', help='print in JSON format')
    # diff
    diff = subp.add_parser('diff', help='diffs two xonsh history files')
    diff_history._create_parser(p=diff)
    # replay, dynamically
    from xonsh import replay
    rp = subp.add_parser('replay', help='replays a xonsh history file')
    replay._create_parser(p=rp)
    _MAIN_ACTIONS['replay'] = replay._main_action
    # gc
    gcp = subp.add_parser(
        'gc', help='launches a new history garbage collector')
    gcp.add_argument('--size', nargs=2, dest='size', default=None,
                     help=('next two arguments represent the history size and '
                           'units; e.g. "--size 8128 commands"'))
    bgcp = gcp.add_mutually_exclusive_group()
    bgcp.add_argument('--blocking', dest='blocking', default=True,
                      action='store_true',
                      help=('ensures that the gc blocks the main thread, '
                            'default True'))
    bgcp.add_argument('--non-blocking', dest='blocking', action='store_false',
                      help='makes the gc non-blocking, and thus return sooner')
    return p


#
# Interface to History
#
class History(object):
    """Xonsh session history."""

    def __init__(self, filename=None, sessionid=None, buffersize=100, gc=True,
                 **meta):
        """Represents a xonsh session's history as an in-memory buffer that is
        periodically flushed to disk.

        Parameters
        ----------
        filename : str, optional
            Location of history file, defaults to
            ``$XONSH_DATA_DIR/xonsh-{sessionid}.json``.
        sessionid : int, uuid, str, optional
            Current session identifier, will generate a new sessionid if not
            set.
        buffersize : int, optional
            Maximum buffersize in memory.
        meta : optional
            Top-level metadata to store along with the history. The kwargs
            'cmds' and 'sessionid' are not allowed and will be overwritten.
        gc : bool, optional
            Run garbage collector flag.
        """
        self.sessionid = sid = uuid.uuid4() if sessionid is None else sessionid
        if filename is None:
            # pylint: disable=no-member
            data_dir = builtins.__xonsh_env__.get('XONSH_DATA_DIR')
            data_dir = os.path.expanduser(data_dir)
            self.filename = os.path.join(
                data_dir, 'xonsh-{0}.json'.format(sid))
        else:
            self.filename = filename
        self.buffer = []
        self.buffersize = buffersize
        self._queue = deque()
        self._cond = Condition()
        self._len = 0
        self.last_cmd_out = None
        self.last_cmd_rtn = None
        meta['cmds'] = []
        meta['sessionid'] = str(sid)
        with open(self.filename, 'w', newline='\n') as f:
            lazyjson.dump(meta, f, sort_keys=True)
        self.gc = HistoryGC() if gc else None
        # command fields that are known
        self.tss = CommandField('ts', self)
        self.inps = CommandField('inp', self)
        self.outs = CommandField('out', self)
        self.rtns = CommandField('rtn', self)

    def __len__(self):
        return self._len

    def append(self, cmd):
        """Appends command to history. Will periodically flush the history to file.

        Parameters
        ----------
        cmd : dict
            Command dictionary that should be added to the ordered history.

        Returns
        -------
        hf : HistoryFlusher or None
            The thread that was spawned to flush history
        """
        opts = builtins.__xonsh_env__.get('HISTCONTROL')
        if ('ignoredups' in opts and len(self) > 0 and
                cmd['inp'] == self.inps[-1]):
            # Skipping dup cmd
            return None
        elif 'ignoreerr' in opts and cmd['rtn'] != 0:
            # Skipping failed cmd
            return None

        self.buffer.append(cmd)
        self._len += 1  # must come before flushing
        if len(self.buffer) >= self.buffersize:
            hf = self.flush()
        else:
            hf = None
        return hf

    def flush(self, at_exit=False):
        """Flushes the current command buffer to disk.

        Parameters
        ----------
        at_exit : bool, optional
            Whether the HistoryFlusher should act as a thread in the
            background, or execute immeadiately and block.

        Returns
        -------
        hf : HistoryFlusher or None
            The thread that was spawned to flush history
        """
        if len(self.buffer) == 0:
            return
        hf = HistoryFlusher(self.filename, tuple(self.buffer), self._queue,
                            self._cond, at_exit=at_exit)
        self.buffer.clear()
        return hf

    @staticmethod
    def show(ns=None, hist=None, start_index=None, end_index=None,
             start_time=None, end_time=None, location=None):
        """
        Show the requested portion of shell history.
        Accepts multiple history sources (xonsh, bash, zsh)

        May be invoked as an alias with `history all/bash/zsh` which will
        provide history as stdout or with `__xonsh_history__.show()`
        which will return the history as a list with each item
        in the tuple form (name, start_time, index).

        If invoked via __xonsh_history__.show() then the ns parameter
        can be supplied as a str with the follow options:
            `all`     - returns xonsh history from all sessions
            `session` - returns xonsh history from current session
            `zsh`     - returns all zsh history
            `bash`    - returns all bash history
        """
        # Check if ns is a string, meaning it was invoked from
        # __xonsh_history__
        alias = True
        valid_formats = {'all': _all_xonsh_formatter,
                         'session': partial(_curr_session_formatter, hist),
                         'zsh': partial(_zsh_hist_formatter, location),
                         'bash': partial(_bash_hist_formatter, location)}
        if isinstance(ns, str) and ns in valid_formats.keys():
            ns = _create_parser().parse_args([ns])
            alias = False
        if not ns:
            ns = _create_parser().parse_args(['all'])
            alias = False
        try:
            commands = valid_formats[ns.action]()
        except KeyError:
            print("{} is not a valid history format".format(ns.action))
            return None
        if not commands:
            return None
        num_of_commands = len(commands)
        digits = len(str(num_of_commands))
        if start_time:
            if isinstance(start_time, datetime):
                start_time = start_time.timestamp()
            if isinstance(start_time, float):
                commands = [c for c in commands if c[1] >= start_time]
            else:
                print("Invalid start time, must be float or datetime.")
        if end_time:
            if isinstance(end_time, datetime):
                end_time = end_time.timestamp()
            if isinstance(end_time, float):
                commands = [c for c in commands if c[1] <= end_time]
            else:
                print("Invalid end time, must be float or datetime.")
        idx = None
        if ns:
            idx = ensure_int_or_slice(ns.n)
            if idx is False:
                return None
            elif isinstance(idx, int):
                try:
                    commands = [commands[idx]]
                except IndexError:
                    err = "Index likely not in range. Only {} commands."
                    print(err.format(len(commands)))
                    return None
        else:
            idx = slice(start_index, end_index)

        if (isinstance(idx, slice) and
                start_time is None and end_time is None):
            commands = commands[idx]

        if ns and ns.reverse:
            commands = reversed(commands)

        if alias:
            for c, t, i in commands:
                print('{:>{width}}: {}'.format(i, c, width=digits + 1))
        else:
            return commands


def _info(ns, hist):
    """Display information about the shell history."""
    data = OrderedDict()
    data['sessionid'] = str(hist.sessionid)
    data['filename'] = hist.filename
    data['length'] = len(hist)
    data['buffersize'] = hist.buffersize
    data['bufferlength'] = len(hist.buffer)
    if ns.json:
        import json
        s = json.dumps(data)
        print(s)
    else:
        lines = ['{0}: {1}'.format(k, v) for k, v in data.items()]
        print('\n'.join(lines))


def _gc(ns, hist):
    """Start and monitor garbage collection of the shell history."""
    hist.gc = gc = HistoryGC(wait_for_shell=False, size=ns.size)
    if ns.blocking:
        while gc.is_alive():
            continue


_MAIN_ACTIONS = {
    'session': History.show,
    'all': History.show,
    'zsh': History.show,
    'bash': History.show,
    'id': lambda ns, hist: print(hist.sessionid),
    'file': lambda ns, hist: print(hist.filename),
    'info': _info,
    'diff': diff_history._main_action,
    'gc': _gc
}


def _main(hist, args):
    """This implements the history CLI."""
    if not args or (args[0] not in _MAIN_ACTIONS and
                    args[0] not in {'-h', '--help'}):
        args.insert(0, 'session')
    if (args[0] in ['session', 'all', 'zsh', 'bash'] and
        len(args) > 1 and args[-1].startswith('-') and
            args[-1][1].isdigit()):
        args.insert(-1, '--')  # ensure parsing stops before a negative int
    ns = _create_parser().parse_args(args)
    if ns.action is None:  # apply default action
        ns = _create_parser().parse_args(['session'] + args)
    _MAIN_ACTIONS[ns.action](ns, hist)


def main(args=None, stdin=None):
    """This is the history command entry point."""
    _ = stdin
    _main(builtins.__xonsh_history__, args)  # pylint: disable=no-member
