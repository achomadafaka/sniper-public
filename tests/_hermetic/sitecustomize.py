# A child Python process started by a test inherits the DNS refusal: tests/conftest.py puts this
# directory first on PYTHONPATH, and Python imports sitecustomize at startup. See hermetic_dns.py.
import hermetic_dns

hermetic_dns.install()
