# -*- coding: utf-8 -*-
import pkg_resources


# We should define the package version external to
# it. http://stackoverflow.com/questions/2058802/how-can-i-get-the-version-defined-in-setup-py-setuptools-in-my-package
version = pkg_resources.require('zygote')[0].version
