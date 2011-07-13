#!/usr/bin/env python
# vim: set fileencoding=utf-8 sw=4 ts=4 et :
# Copyright (C) 2006-2011 CS-SI
# License: GNU GPL v2 <http://www.gnu.org/licenses/gpl-2.0.html>

import os, sys
from setuptools import setup, find_packages

sysconfdir = os.getenv("SYSCONFDIR", "/etc")
localstatedir = os.getenv("LOCALSTATEDIR", "/var")

tests_require = [
    'coverage',
    'nose',
    'pylint',
    'mock',
]

def install_i18n(i18ndir, destdir):
    data_files = []
    langs = []
    for f in os.listdir(i18ndir):
        if os.path.isdir(os.path.join(i18ndir, f)) and not f.startswith("."):
            langs.append(f)
    for lang in langs:
        for f in os.listdir(os.path.join(i18ndir, lang, "LC_MESSAGES")):
            if f.endswith(".mo"):
                data_files.append(
                        (os.path.join(destdir, lang, "LC_MESSAGES"),
                         [os.path.join(i18ndir, lang, "LC_MESSAGES", f)])
                )
    return data_files

setup(name='vigilo-correlator',
        version='2.0.3',
        author='Vigilo Team',
        author_email='contact@projet-vigilo.org',
        url='http://www.projet-vigilo.org/',
        license='http://www.gnu.org/licenses/gpl-2.0.html',
        description="Vigilo correlator",
        long_description="The Vigilo correlation engine aggregates alerts "
                         "to reduce information overload and help point "
                         "out the cause of a problem.",
        zip_safe=False, # pour pouvoir écrire le dropin.cache de twisted
        install_requires=[
            'setuptools',
            'lxml',
            'python-memcached',
            'vigilo-models',
            'vigilo-pubsub',
            'vigilo-connector',
            'networkx',
            'ampoule',
            ],
        namespace_packages = [
            'vigilo',
            ],
        packages=find_packages("src")+["twisted"],
        package_data={'twisted': ['plugins/vigilo_correlator.py']},
        message_extractors={
            'src': [
                ('**.py', 'python', None),
            ],
        },
        extras_require={
            'tests': tests_require,
        },
        entry_points={
            'console_scripts': [
                'vigilo-correlator = twisted.scripts.twistd:run',
                ],
        },
        package_dir={'': 'src'},
        data_files=[
                    (os.path.join(sysconfdir, "vigilo/correlator"),
                        ["settings.ini"]),
                    (os.path.join(localstatedir, "lib/vigilo/correlator"), []),
                    (os.path.join(localstatedir, "run/vigilo-correlator"), []),
                   ] + install_i18n("i18n", os.path.join(sys.prefix, 'share', 'locale')),
        )

