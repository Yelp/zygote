#!/usr/bin/env python

from setuptools import find_packages
from setuptools import setup


setup(
    name         = 'zygote',
    version      = '0.5.3',
    author       = 'Evan Klitzke',
    author_email = 'evan@eklitzke.org',
    description  = 'A tornado HTTP worker management tool',
    license      = 'Apache License 2.0',
    entry_points = {'console_scripts': 'zygote = zygote.main:main'},
    packages     = find_packages(exclude=['tests']),
    install_requires = ['setuptools', 'tornado'],
    include_package_data = True,
)
