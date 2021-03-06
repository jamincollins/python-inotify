#!/usr/bin/env python

import distutils.core
import distutils.util


platform = distutils.util.get_platform()

if not platform.startswith('linux'):
    raise Exception('inotify is linux-specific, and does not work on %s' %
                    platform)

distutils.core.setup(
    name='python-inotify',
    version='0.6-test',
    description='Interface to Linux inotify subsystem',
    author="Jan Kanis",
    author_email='jan.code@jankanis.nl',
    license='LGPL',
    platforms='Linux',
    packages=['inotify'],
    url='https://bitbucket.org/JanKanis/python-inotify',
    classifiers=['Development Status :: 4 - Beta',
                 'Intended Audience :: Developers',
                 'License :: OSI Approved :: GNU General Public License v2 (GPLv2)',
                 'Operating System :: POSIX :: Linux',
                 'Programming Language :: Python',
                 'Programming Language :: Python :: 2',
                 'Programming Language :: Python :: 3',
                 'Topic :: System :: Archiving',
                 'Topic :: System :: Filesystems',
                 'Topic :: System :: Monitoring'],
    ext_modules=[distutils.core.Extension('inotify._inotify',
                                          ['inotify/_inotify.c'])],
    )


