from setuptools import setup, find_packages

with open("requirements.txt") as f:
	install_requires = f.read().strip().split("\n")

# get version from __version__ variable in slife/__init__.py
from slife import __version__ as version

setup(
	name="slife",
	version=version,
	description="Woocommerce Sales Order creation web hook supporting variant and non-variant items and outsourcing",
	author="Richard Case",
	author_email="support@casesolved.co.uk",
	license='Proprietary',
	packages=find_packages(),
	zip_safe=False,
	include_package_data=True,
	install_requires=install_requires
)
