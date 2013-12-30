#!/usr/bin/env python

from setuptools import find_packages


setup(
    name         = 'zygote',
    version      = '0.5.3',
    author       = 'Evan Klitzke',
    author_email = 'evan@eklitzke.org',
    description  = 'A tornado HTTP worker management tool',
    license      = 'Apache License 2.0',
    entry_points = {'console_scripts': 'zygote = zygote.main:main'},
    install_requires = ['setuptools', 'tornado']
)
