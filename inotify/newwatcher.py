# watcher.py - high-level interfaces to the Linux inotify subsystem

# Copyright 2006 Bryan O'Sullivan <bos@serpentine.com>
# Copyright 2012-2013 Jan Kanis <jan.code@jankanis.nl>

# This library is free software; you can redistribute it and/or modify
# it under the terms of version 2.1 of the GNU Lesser General Public
# License, incorporated herein by reference.

# Additionally, code written by Jan Kanis may also be redistributed and/or 
# modified under the terms of any version of the GNU Lesser General Public 
# License greater than 2.1. 

'''High-level interfaces to the Linux inotify subsystem.

The inotify subsystem provides an efficient mechanism for file status
monitoring and change notification.

The Watcher class hides the low-level details of the inotify
interface, and provides a Pythonic wrapper around it.  It generates
events that provide somewhat more information than raw inotify makes
available.

The AutoWatcher class is more useful, as it automatically watches
newly-created directories on your behalf.'''

__author__ = "Jan Kanis <jan.code@jankanis.nl>"

from . import constants
from . import _inotify as inotify
import functools
import operator
import array
import errno
import fcntl
import os
from os import path
import termios
from collections import namedtuple


# Inotify flags that can be specified on a watch and can be returned in an event
_inotify_props = {
    'access': 'File was accessed',
    'modify': 'File was modified',
    'attrib': 'Attribute of a directory entry was changed',
    'close': 'File was closed',
    'close_write': 'File was closed after being written to',
    'close_nowrite': 'File was closed without being written to',
    'open': 'File was opened',
    'move': 'Directory entry was renamed',
    'moved_from': 'Directory entry was renamed from this name',
    'moved_to': 'Directory entry was renamed to this name',
    'create': 'Directory entry was created',
    'delete': 'Directory entry was deleted',
    'delete_self': 'The watched directory entry was deleted',
    'move_self': 'The watched directory entry was renamed',
    'link_changed': 'The named path no longer resolves to the same file',
    }

# Inotify flags that can only be returned in an event
_event_props = {
    'unmount': 'Directory was unmounted, and can no longer be watched',
    'q_overflow': 'Kernel dropped events due to queue overflow',
    'ignored': 'Directory entry is no longer being watched',
    'isdir': 'Event occurred on a directory',
    }
_event_props.update(_inotify_props)

# Inotify flags that can only be specified in a watch
_watch_props = {
    'dont_follow': "Don't dereference pathname if it is a symbolic link",
    'excl_unlink': "Don't generate events after the file has been unlinked",
    }
_watch_props.update(_inotify_props)


# TODO: move this to __init__.py

inotify_builtin_constants = functools.reduce(operator.or_, constants.values())
inotify.IN_LINK_CHANGED = 1
while inotify.IN_LINK_CHANGED < inotify_builtin_constants:
    inotify.IN_LINK_CHANGED <<= 1
constants['IN_LINK_CHANGED'] = inotify.IN_LINK_CHANGED

def decode_mask(mask):
    d = inotify.decode_mask(mask & inotify_builtin_constants)
    if mask & inotify.IN_LINK_CHANGED:
        d.append('IN_LINK_CHANGED')
    return d



def _make_getter(name, doc):
    def getter(self, mask=constants['IN_' + name.upper()]):
        return self.mask & mask
    getter.__name__ = name
    getter.__doc__ = doc
    return getter





class Event(object):
    '''Derived inotify event class.

    The following fields are available:

        mask: event mask, indicating what kind of event this is

        cookie: rename cookie, if a rename-related event

        fullpath: the full path of the file or directory to which the event
        occured. If this watch has more than one path, a path is chosen
        arbitrarily.

        paths: a list of paths that resolve to the watched file/directory

        name: name of the directory entry to which the event occurred
        (may be None if the event happened to a watched directory)

        wd: watch descriptor that triggered this event

    '''

    __slots__ = (
        'cookie',
        'mask',
        'name',
        'raw',
        'path',
        )

    @property
    def fullpath(self):
        if self.name:
            return path.join(self.path, self.name)
        return self.path

    def __init__(self, raw, path):
        self.raw = raw
        self.path = path
        self.mask = raw.mask
        self.cookie = raw.cookie
        self.name = raw.name
    
    def __repr__(self):
        r = 'Event(path={}, mask={}'.format(repr(self.path), '|'.join(decode_mask(self.mask)))
        if self.cookie:
            r += ', cookie={}'.format(self.cookie)
        if self.name:
            r += ', name={}'.format(repr(self.name))
        r += ')'
        return r


for name, doc in _event_props.items():
    setattr(Event, name, property(_make_getter(name, doc), doc=doc))





class Watcher (object):

    def __init__(self):
        self.fd = inotify.init()
        self._watchdescriptors = {}
        self._paths = {}
        self._buffer = []

    def add(self, pth, mask):
        if pth in self._paths:
            self._paths[pth].update_mask(mask)
        self._paths[pth] = _Watch(self, pth, mask)
        return self._paths[pth]

    def _createwatch(self, pth, name, mask, callback):
        wd = inotify.add_watch(self.fd, pth, mask)
        if not wd in self._watchdescriptors:
            self._watchdescriptors[wd] = _Descriptor(self, wd)
        self._watchdescriptors[wd].add_callback(pth, mask, name, callback)
        return self._watchdescriptors[wd]

    def _removewatch(self, descriptor):
        del self._watchdescriptors[descriptor.wd]

    def read(self, block=True, bufsize=None, store_events=False):
        '''Read a list of queued inotify events.

        If bufsize is zero, only return those events that can be read
        immediately without blocking.  Otherwise, block until events are
        available.'''

        if self._buffer:
            b, self._buffer = self._buffer, []
            return b

        if not block:
            bufsize = 0
        elif bufsize == 0:
            bufsize = None

        if not len(self._watchdescriptors):
            raise NoFilesException("There are no files to watch")

        events = []
        for evt in inotify.read(self.fd, bufsize):
            for e in self._watchdescriptors[evt.wd].handle_event(evt):
                events.append(e)
        if store_events:
            self._buffer.extend(events)
            return
        else:
            return events

    def close(self):
        os.close(self.fd)


class _Watch (object):
    def __init__(self, watcher, pth, mask):
        self.watcher = watcher
        self.path = self._normpath(pth)
        self.cwd = os.getcwd()
        self.mask = mask
        self.links = []
        self.inode = None
        self.add(pth)

    def _normpath(self, pth):
        return [p for p in pth.split(path.sep) if p not in ('', '.')]

    def _nonrel(self, pth):
        '''Return the path joined with the working directory at the time this watch was
        created.
        '''
        return path.join(self.cwd, pth)

    def add(self, pth):
        # Register symlinks in a non-racy way
        linkdepth = 0
        while True:
            try:
                link = os.readlink(pth)
                self.addlink(pth)
                pth = os.join(path.dirname(pth), link)
                linkdepth += 1
            except OSError as e:
                if e.errno == os.errno.EINVAL:
                    # The entry is not a symbolic link
                    break
                if e.errno in (os.errno.ENOENT, os.errno.ENOTDIR):
                    # The entry does not exist, or the path is not valid
                    if linkdepth == 0:
                        raise InotifyWatcherException("File does not exist: "+pth)
                    # the originally passed path exists, but it is a broken symlink
                    return
                raise

        self.addleaf(pth)

    def addleaf(self, pth):
        mask = self.mask | inotify.IN_MOVE_SELF | inotify.IN_DELETE_SELF
        self.links.append(_Link(len(self.links), self, mask, pth, None))
        st = os.stat(pth)
        self.inode = (st.st_dev, st.st_ino)
                    
    def addlink(self, pth):
        pth, name = path.split(pth)
        mask = inotify.IN_MOVE | inotify.IN_DELETE | inotify.IN_CREATE | inotify.IN_ONLYDIR
        self.links.append(_Link(len(self.links), self, mask, pth, name))
        
    def handle_event(self, event, pth):
        if pth.idx == len(self.links) - 1 and event.mask & self.mask:
            yield Event(event, path.join(*self.path))
        else:
            yield Event(semirawevent(mask=inotify.IN_LINK_CHANGED, cookie=0, name=None, wd=event.wd), path.join(*self.path))
            

semirawevent = namedtuple('semirawevent', 'mask cookie name wd')


class _Link (object):
    def __init__(self, idx, watch, mask, pth, name):
        self.idx = idx
        self.watch = watch
        self.mask = mask
        self.path = pth
        self.name = name
        self.wd = watch.watcher._createwatch(self.path, self.name, mask, self.handle_event)

    def handle_event(self, event):
        yield from self.watch.handle_event(event, self)
    

class _Descriptor (object):

    def __init__(self, watcher, wd):
        self.watcher = watcher
        self.wd = wd
        self.mask = 0
        self.callbacks = []

    def add_callback(self, pth, mask, name, callback):
        self.mask |= mask
        self.callbacks.append((mask, name, callback))

    def handle_event(self, event):
        for m, n, c in self.callbacks:
            if event.mask & m and (n == None or n == event.name):
                yield from c(event)
        if event.mask & inotify.IN_IGNORED:
            self.watcher._removewatch(self)
      
